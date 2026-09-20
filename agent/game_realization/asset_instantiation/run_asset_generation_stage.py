import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMMON_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "shared_representation"))
if COMMON_DIR not in sys.path:
    sys.path.insert(0, COMMON_DIR)

from staging_paths import get_demo_name, get_staging_root


PROJECT_ROOT = Path(
    os.environ.get("CODE2WORLDS_ROOT")
    or Path(__file__).resolve().parents[3]
)
STAGING_ROOT = Path(get_staging_root(PROJECT_ROOT))
DEFAULT_ASSET_PLAN = STAGING_ROOT / "asset_realization/asset_plan.json"
OUTPUT_DIR = STAGING_ROOT / "asset_generation"
REFERENCE_IMAGE_DIR = OUTPUT_DIR / "reference_images"
HUNYUAN_RAW_DIR = OUTPUT_DIR / "hunyuan_raw"
MANIFEST_PATH = OUTPUT_DIR / "asset_generation_manifest.json"
LOG_PATH = OUTPUT_DIR / "asset_generation.log"
GENERATED_GLB_DIR = PROJECT_ROOT / "assets/generated_glb"
if get_demo_name():
    GENERATED_GLB_DIR = GENERATED_GLB_DIR / get_demo_name()

DASHSCOPE_IMAGE_ENDPOINT = os.environ.get(
    "DASHSCOPE_IMAGE_ENDPOINT",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
)
DASHSCOPE_IMAGE_MODEL = "qwen-image-2.0-pro"
DASHSCOPE_IMAGE_SIZE = "2048*2048"
DEFAULT_T2I_BACKEND = "dashscope_sdk_qwen_image"
DEFAULT_HUNYUAN_REPO = os.environ.get(
    "HUNYUAN3D_REPO",
    str(PROJECT_ROOT.parent / "Hunyuan3D-2.1"),
)
DEFAULT_HUNYUAN_MODEL_PATH = os.environ.get(
    "HUNYUAN3D_MODEL_PATH",
    "tencent/Hunyuan3D-2.1",
)
DEFAULT_HUNYUAN_SUBFOLDER = "hunyuan3d-dit-v2-1"
DEFAULT_HUNYUAN_TEXGEN_MODEL_PATH = "tencent/Hunyuan3D-2.1"
DINO_V2_MODEL_PATH = os.environ.get(
    "DINOV2_MODEL_PATH",
    "facebook/dinov2-giant",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset_plan", default=str(DEFAULT_ASSET_PLAN))
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--hunyuan_backend", choices=["gradio", "local"], default="gradio")
    parser.add_argument("--hunyuan_repo", default=DEFAULT_HUNYUAN_REPO)
    parser.add_argument("--model_path", default=DEFAULT_HUNYUAN_MODEL_PATH)
    parser.add_argument("--subfolder", default=DEFAULT_HUNYUAN_SUBFOLDER)
    parser.add_argument("--texgen_model_path", default=DEFAULT_HUNYUAN_TEXGEN_MODEL_PATH)
    parser.add_argument("--skip_existing", action="store_true")
    parser.add_argument("--reuse_reference_images", action="store_true")
    parser.add_argument("--reference_images_only", action="store_true", help="generate reference images and stop before Hunyuan3D")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=999)
    parser.add_argument("--t2i_backend", default=DEFAULT_T2I_BACKEND)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--guidance_scale", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--octree_resolution", type=int, default=256)
    parser.add_argument("--num_chunks", type=int, default=8000)
    parser.add_argument("--export_texture", action="store_true")
    parser.add_argument("--tex_resolution", type=int, default=512)
    parser.add_argument("--tex_max_num_view", type=int, default=6)
    parser.add_argument("--reduce_face", action="store_true")
    parser.add_argument("--target_face_num", type=int, default=50000)
    return parser.parse_args()


def set_safe_cache_env():
    cache_root = Path(
        os.environ.get("CODE2GAMES_CACHE_DIR")
        or os.environ.get("XDG_CACHE_HOME")
        or Path.home() / ".cache"
    ).expanduser()
    env_defaults = {
        "HF_HOME": cache_root / "huggingface",
        "HUGGINGFACE_HUB_CACHE": cache_root / "huggingface/hub",
        "TRANSFORMERS_CACHE": cache_root / "huggingface/transformers",
        "TORCH_HOME": cache_root / "torch",
        "XDG_CACHE_HOME": cache_root,
        "U2NET_HOME": cache_root / "u2net",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "DINOV2_MODEL_PATH": DINO_V2_MODEL_PATH,
    }
    for key, value in env_defaults.items():
        os.environ[key] = str(value)
        if key not in {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "DINOV2_MODEL_PATH"}:
            Path(os.environ[key]).mkdir(parents=True, exist_ok=True)


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    HUNYUAN_RAW_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_GLB_DIR.mkdir(parents=True, exist_ok=True)


def log(message, *values):
    text = " ".join([str(message), *[str(v) for v in values]])
    print(text)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def log_block(text):
    text = str(text)
    print(text)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def sanitize_filename(value):
    value = re.sub(r"[^\w.\-]+", "_", str(value), flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("._")
    return value[:120] or "asset"


def resolve_project_path(path_value):
    path = Path(str(path_value))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def is_valid_glb(path):
    return path.exists() and path.is_file() and path.stat().st_size > 1024


def inspect_glb_flat_base(path, asset):
    """Detect one continuous unintended floor/base near the mesh minimum Z.

    Aggregate bottom area alone is not sufficient: four separated tire contact
    patches can cover a large part of a vehicle footprint without forming a
    floor.  Evaluate connected bottom-face components so distributed supports
    remain valid while a single broad slab is still rejected.
    """
    try:
        import trimesh
    except Exception as exc:
        raise RuntimeError(f"trimesh is required for GLB base inspection: {exc}") from exc

    loaded = trimesh.load(str(path), force="scene", process=False)
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise RuntimeError(f"GLB base inspection found no mesh geometry: {path}")
        mesh = loaded.dump(concatenate=True)
    else:
        mesh = loaded
    if mesh is None or len(mesh.vertices) < 3 or len(mesh.faces) < 1:
        raise RuntimeError(f"GLB base inspection found an empty mesh: {path}")

    bounds = mesh.bounds
    extents = bounds[1] - bounds[0]
    width = float(extents[0])
    depth = float(extents[1])
    height = float(extents[2])
    footprint_area = max(width * depth, 1e-12)
    if height <= 1e-8:
        raise RuntimeError(f"GLB base inspection found degenerate height: {path}")

    bottom_band_height = max(height * 0.06, 1e-6)
    bottom_limit = float(bounds[0][2]) + bottom_band_height
    triangles = mesh.triangles
    face_normals = mesh.face_normals
    face_areas = mesh.area_faces
    bottom_faces = triangles[:, :, 2].max(axis=1) <= bottom_limit
    horizontal_faces = abs(face_normals[:, 2]) >= 0.94
    selected_faces = bottom_faces & horizontal_faces
    projected_bottom_area = float(
        (face_areas[selected_faces] * abs(face_normals[selected_faces, 2])).sum()
    )
    projected_area_ratio = projected_bottom_area / footprint_area

    bottom_vertices = mesh.vertices[mesh.vertices[:, 2] <= bottom_limit]
    if len(bottom_vertices):
        bottom_span = bottom_vertices.max(axis=0) - bottom_vertices.min(axis=0)
        bottom_span_ratio = float(bottom_span[0] * bottom_span[1]) / footprint_area
    else:
        bottom_span_ratio = 0.0

    selected_indices = set(int(index) for index in selected_faces.nonzero()[0])
    adjacency = {index: [] for index in selected_indices}
    if selected_indices:
        for first, second in mesh.face_adjacency:
            first = int(first)
            second = int(second)
            if first in selected_indices and second in selected_indices:
                adjacency[first].append(second)
                adjacency[second].append(first)

    components = []
    unseen = set(selected_indices)
    while unseen:
        seed = unseen.pop()
        component = [seed]
        stack = [seed]
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.append(neighbor)
                    stack.append(neighbor)
        components.append(component)

    component_metrics = []
    for component in components:
        component_area = float(
            (face_areas[component] * abs(face_normals[component, 2])).sum()
        )
        component_triangles = triangles[component].reshape((-1, 3))
        component_span = component_triangles.max(axis=0) - component_triangles.min(axis=0)
        component_metrics.append({
            "projected_area_ratio": component_area / footprint_area,
            "span_ratio": float(component_span[0] * component_span[1]) / footprint_area,
        })

    continuous_base_components = [
        item for item in component_metrics
        if item["projected_area_ratio"] >= 0.30 and item["span_ratio"] >= 0.55
    ]
    detected = bool(continuous_base_components)
    largest_component_area_ratio = max(
        (item["projected_area_ratio"] for item in component_metrics),
        default=0.0,
    )
    largest_component_span_ratio = max(
        (item["span_ratio"] for item in component_metrics),
        default=0.0,
    )
    policy = str(asset.get("flat_base_policy") or "reject")
    if policy not in {"allow", "reject"}:
        raise ValueError("flat_base_policy must be allow or reject for %s" % asset.get("asset_id"))
    rejected = bool(detected and policy == "reject")
    return {
        "checked": True,
        "policy": policy,
        "policy_reason": str(asset.get("flat_base_reason") or "legacy default: reject unintended flat base"),
        "detected": bool(detected),
        "rejected": rejected,
        "projected_bottom_area_ratio": round(projected_area_ratio, 6),
        "bottom_span_ratio": round(bottom_span_ratio, 6),
        "bottom_component_count": len(component_metrics),
        "continuous_base_component_count": len(continuous_base_components),
        "largest_component_projected_area_ratio": round(largest_component_area_ratio, 6),
        "largest_component_span_ratio": round(largest_component_span_ratio, 6),
        "bottom_band_height_ratio": 0.06,
        "horizontal_normal_threshold": 0.94,
        "projected_area_threshold": 0.30,
        "bottom_span_threshold": 0.55,
        "mesh_extents": [round(width, 6), round(depth, 6), round(height, 6)],
        "vertex_count": int(len(mesh.vertices)),
        "face_count": int(len(mesh.faces)),
    }


def enforce_glb_base_check(path, asset):
    result = inspect_glb_flat_base(path, asset)
    log(
        "GLB_BASE_CHECK",
        asset["asset_id"],
        f"detected={int(result['detected'])}",
        f"rejected={int(result['rejected'])}",
        f"policy={result['policy']}",
        f"area_ratio={result['projected_bottom_area_ratio']}",
        f"span_ratio={result['bottom_span_ratio']}",
        f"components={result['bottom_component_count']}",
        f"continuous_components={result['continuous_base_component_count']}",
        f"largest_component_area={result['largest_component_projected_area_ratio']}",
        f"largest_component_span={result['largest_component_span_ratio']}",
    )
    if result["rejected"]:
        raise RuntimeError(
            "probable unintended flat base detected in GLB "
            f"(projected_bottom_area_ratio={result['projected_bottom_area_ratio']}, "
            f"bottom_span_ratio={result['bottom_span_ratio']})"
        )
    return result


def is_valid_reference_image(path):
    return path.exists() and path.is_file() and path.stat().st_size > 1024


def load_assets(asset_plan):
    if not isinstance(asset_plan.get("assets"), list):
        raise ValueError("asset_plan must contain new-schema assets list: asset_plan['assets']")
    assets = asset_plan["assets"]

    required = ["asset_id", "asset_name", "generation_prompt_en", "expected_glb_path"]
    normalized = []
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ValueError(f"asset at index {index} is not an object")
        missing = [key for key in required if not asset.get(key)]
        if missing:
            raise ValueError(f"asset at index {index} missing required field(s): {missing}")
        normalized.append(asset)
    return normalized


def build_reference_prompt(asset):
    prompt = str(asset.get("generation_prompt_en", "")).strip()
    return (
        "REFERENCE IMAGE COMPOSITION REQUIREMENTS: Create a single clean product reference for image-to-3D generation, "
        "not an environment concept image. Show exactly one standalone 3D game prop centered in the frame against a plain pure-white seamless studio background. "
        "Do not depict a forest, orchard, landscape, terrain, surrounding trees, surrounding plants, or a natural ground scene. "
        "Any environment or palette language in the object description is material guidance for the prop itself only. "
        "Use neutral shadowless studio lighting, full object visible, elevated three-quarter product view, with empty white margin around the complete silhouette. "
        "Show only the object's own natural lowest contact edge. Never add a support base, floor, board, platform, pedestal, plinth, tile, slab, disk, square, rectangle, or triangle beneath it. "
        "The background must remain uniformly white with no horizon, floor plane, cast shadow, contact shadow, reflection, or lighting gradient. "
        "For an airborne object, preserve clear empty background space underneath it and never invent feet or support geometry. "
        "The prop must have unmistakable three-dimensional volume: substantial height and depth, readable front, side, rear, top and underside surfaces, and a strong silhouette in the three-quarter view. "
        "Never turn a hazard, goal, landmark, safe-zone marker, or event cue into a flat ground patch, decal, painted mark, texture sheet, thin tile, mat, puddle, or shallow relief. The invisible gameplay zone is implemented separately at runtime. "
        "No character, no person, no text, no labels, no watermark, no UI, no scene background, no cropped parts.\n\n"
        f"OBJECT DESIGN: {prompt}\n\n"
        "FINAL COMPOSITION CHECK: exactly one isolated prop, uniform pure-white background, no environment, no shadow, no floor or base, and no extra objects."
    )


def build_negative_prompt(asset):
    base = str(asset.get("negative_prompt_en", "") or "").strip()
    extra = (
        "people, character, human, hands, text, letters, watermark, logo, subtitle, UI, "
        "busy background, landscape scene, forest background, orchard background, trees in background, terrain, natural ground plane, "
        "floor, support board, base, pedestal, platform, plinth, tile, slab, disk, square, rectangle, triangle, cast shadow, contact shadow, reflection, "
        "surrounding vegetation, environmental scene, multiple objects, cropped object, blurry, low quality, distorted geometry"
    )
    return f"{base}, {extra}" if base else extra


def http_json_post(url, payload, api_key):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DashScope HTTP {exc.code}: {detail}") from exc


def extract_dashscope_image_url(response):
    output = get_field(response, "output", {})
    choices = get_field(output, "choices", [])
    for choice in choices:
        message = get_field(choice, "message", {})
        content = get_field(message, "content", [])
        if isinstance(content, list):
            for item in content:
                image_url = get_field(item, "image")
                if image_url:
                    return image_url
        else:
            image_url = get_field(content, "image")
            if image_url:
                return image_url
    raise RuntimeError(f"DashScope response did not contain an image URL: {response}")


def get_field(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def serialize_response(response):
    if isinstance(response, (dict, list, str, int, float, bool)) or response is None:
        return response
    for method_name in ("model_dump", "to_dict"):
        method = getattr(response, method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return {"response": repr(response)}


def download_file(url, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as response:
        with open(output_path, "wb") as f:
            shutil.copyfileobj(response, f)
    if not output_path.exists() or output_path.stat().st_size <= 1024:
        raise RuntimeError(f"downloaded file is missing or too small: {output_path}")


def generate_reference_image_dashscope_sdk(asset, output_path):
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope SDK Qwen-Image generation")
    try:
        from dashscope import MultiModalConversation
    except Exception as exc:
        raise RuntimeError(f"dashscope SDK is required for DashScope image generation: {exc}") from exc

    final_prompt = build_reference_prompt(asset)
    messages = [
        {
            "role": "user",
            "content": [
                {"text": final_prompt},
            ],
        }
    ]
    response = MultiModalConversation.call(
        api_key=api_key,
        model=DASHSCOPE_IMAGE_MODEL,
        messages=messages,
        result_format="message",
        stream=False,
        watermark=False,
        prompt_extend=False,
        negative_prompt=build_negative_prompt(asset),
        size=DASHSCOPE_IMAGE_SIZE,
    )
    raw_path = HUNYUAN_RAW_DIR / f"{sanitize_filename(asset['asset_id'])}_dashscope_sdk_response.json"
    write_json(raw_path, serialize_response(response))

    if get_field(response, "status_code") != 200:
        raise RuntimeError(
            "DashScope image generation failed: "
            f"status={get_field(response, 'status_code')}, "
            f"code={get_field(response, 'code')}, "
            f"message={get_field(response, 'message')}"
        )

    image_url = extract_dashscope_image_url(response)
    download_file(image_url, output_path)
    return {"source": "dashscope_sdk", "url": image_url}


def generate_reference_image_dashscope(asset, output_path):
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope Qwen-Image text-to-image generation")

    payload = {
        "model": DASHSCOPE_IMAGE_MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": build_reference_prompt(asset)}],
                }
            ]
        },
        "parameters": {
            "negative_prompt": build_negative_prompt(asset),
            "prompt_extend": False,
            "watermark": False,
            "size": DASHSCOPE_IMAGE_SIZE,
        },
    }
    response = http_json_post(DASHSCOPE_IMAGE_ENDPOINT, payload, api_key)
    raw_path = HUNYUAN_RAW_DIR / f"{sanitize_filename(asset['asset_id'])}_dashscope_response.json"
    write_json(raw_path, response)
    image_url = extract_dashscope_image_url(response)
    download_file(image_url, output_path)
    return image_url


def generate_reference_image(asset, output_path, backend):
    if backend == "dashscope_sdk_qwen_image":
        return generate_reference_image_dashscope_sdk(asset, output_path)
    if backend in {"dashscope_native", "dashscope_qwen_image"}:
        return generate_reference_image_dashscope(asset, output_path)
    raise ValueError(
        "unsupported text-to-image backend: "
        f"{backend}. Use dashscope_sdk_qwen_image or dashscope_native."
    )


def extract_file_path(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("path"):
            return value["path"]
        if value.get("name"):
            return value["name"]
    if isinstance(value, (list, tuple)):
        for item in value:
            result = extract_file_path(item)
            if result:
                return result
    return None


class LocalHunyuanBackend:
    def __init__(self, args):
        self.args = args
        self.repo = Path(args.hunyuan_repo)
        self.save_dir = HUNYUAN_RAW_DIR / "local_hunyuan"
        self.current_dir = self.repo
        self.html_height = 650
        self.html_width = 500
        self.device = "cuda"

        if not self.repo.exists():
            raise RuntimeError(f"local Hunyuan repo does not exist: {self.repo}")

        self._prepare_imports()
        self._load_modules()
        self._load_pipelines()
        log("LOCAL_PIPELINE_LOADED", self.repo)

    def _prepare_imports(self):
        for path in (self.repo, self.repo / "hy3dshape", self.repo / "hy3dpaint"):
            path_text = str(path)
            if path_text not in sys.path:
                sys.path.insert(0, path_text)
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def _load_modules(self):
        try:
            import torch
            import trimesh
            from PIL import Image
            from hy3dpaint.convert_utils import create_glb_with_pbr_materials
            from hy3dpaint.textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline
            from hy3dshape import DegenerateFaceRemover, FaceReducer, FloaterRemover, Hunyuan3DDiTFlowMatchingPipeline
            from hy3dshape.pipelines import export_to_trimesh
            from hy3dshape.rembg import BackgroundRemover
        except Exception as exc:
            raise RuntimeError(f"failed to import local Hunyuan3D modules from {self.repo}: {exc}") from exc

        self.torch = torch
        self.trimesh = trimesh
        self.Image = Image
        self.create_glb_with_pbr_materials = create_glb_with_pbr_materials
        self.Hunyuan3DPaintConfig = Hunyuan3DPaintConfig
        self.Hunyuan3DPaintPipeline = Hunyuan3DPaintPipeline
        self.DegenerateFaceRemover = DegenerateFaceRemover
        self.FaceReducer = FaceReducer
        self.FloaterRemover = FloaterRemover
        self.Hunyuan3DDiTFlowMatchingPipeline = Hunyuan3DDiTFlowMatchingPipeline
        self.export_to_trimesh = export_to_trimesh
        self.BackgroundRemover = BackgroundRemover

        log("CUDA_VISIBLE_DEVICES", os.environ.get("CUDA_VISIBLE_DEVICES"))
        if torch.cuda.is_available():
            log("torch.cuda.current_device()", torch.cuda.current_device())
            log("torch.cuda.get_device_name()", torch.cuda.get_device_name(torch.cuda.current_device()))
        else:
            raise RuntimeError("local Hunyuan backend requires CUDA, but torch.cuda.is_available() is False")

    def _load_pipelines(self):
        try:
            conf = self.Hunyuan3DPaintConfig(
                max_num_view=self.args.tex_max_num_view,
                resolution=self.args.tex_resolution,
            )
            conf.realesrgan_ckpt_path = str(self.repo / "hy3dpaint/ckpt/RealESRGAN_x4plus.pth")
            conf.multiview_cfg_path = str(self.repo / "hy3dpaint/cfgs/hunyuan-paint-pbr.yaml")
            conf.custom_pipeline = str(self.repo / "hy3dpaint/hunyuanpaintpbr")
            conf.multiview_pretrained_path = self.args.texgen_model_path
            conf.dino_ckpt_path = DINO_V2_MODEL_PATH
            if hasattr(conf, "model_path"):
                conf.model_path = self.args.texgen_model_path
            if hasattr(conf, "dinov2_model_path"):
                conf.dinov2_model_path = DINO_V2_MODEL_PATH
            self.tex_pipeline = self.Hunyuan3DPaintPipeline(conf)
        except Exception as exc:
            raise RuntimeError(f"failed to load local Hunyuan texture pipeline: {exc}") from exc

        try:
            self.rmbg_worker = self.BackgroundRemover()
            self.i23d_worker = self.Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
                self.args.model_path,
                subfolder=self.args.subfolder,
                use_safetensors=False,
                device=self.device,
            )
            self.floater_remove_worker = self.FloaterRemover()
            self.degenerate_face_remove_worker = self.DegenerateFaceRemover()
            self.face_reduce_worker = self.FaceReducer()
        except Exception as exc:
            raise RuntimeError(f"failed to load local Hunyuan shape pipeline: {exc}") from exc

    def gen_save_folder(self, max_size=200):
        dirs = [path for path in self.save_dir.iterdir() if path.is_dir()]
        if len(dirs) >= max_size:
            shutil.rmtree(min(dirs, key=lambda path: path.stat().st_ctime))
        save_folder = self.save_dir / str(uuid.uuid4())
        save_folder.mkdir(parents=True, exist_ok=True)
        return save_folder

    def export_mesh(self, mesh, save_folder, textured=False, file_type="glb"):
        filename = "textured_mesh" if textured else "white_mesh"
        path = Path(save_folder) / f"{filename}.{file_type}"
        if file_type in {"glb", "obj"}:
            mesh.export(path, include_normals=textured)
        else:
            mesh.export(path)
        return str(path)

    def build_model_viewer_html(self, save_folder, textured=False):
        return str(Path(save_folder) / ("textured_mesh.glb" if textured else "white_mesh.glb"))

    def quick_convert_with_obj2gltf(self, obj_path, glb_path):
        textures = {
            "albedo": str(obj_path).replace(".obj", ".jpg"),
            "metallic": str(obj_path).replace(".obj", "_metallic.jpg"),
            "roughness": str(obj_path).replace(".obj", "_roughness.jpg"),
        }
        self.create_glb_with_pbr_materials(str(obj_path), textures, str(glb_path))
        return str(glb_path)

    def generate(self, reference_image_path, args):
        log("LOCAL_GENERATION_START", reference_image_path)
        start_time_0 = time.time()
        image = self.Image.open(reference_image_path).convert("RGBA")
        save_folder = self.gen_save_folder()
        stats = {
            "model": {
                "shapegen": f"{args.model_path}/{args.subfolder}",
                "texgen": args.texgen_model_path,
            },
            "params": {
                "steps": args.steps,
                "guidance_scale": args.guidance_scale,
                "seed": args.seed,
                "octree_resolution": args.octree_resolution,
                "check_box_rembg": True,
                "num_chunks": args.num_chunks,
            },
            "time": {},
        }

        start_time = time.time()
        alpha_min, alpha_max = image.getchannel("A").getextrema()
        if alpha_min < 255:
            log("PRESERVE_REFERENCE_ALPHA", reference_image_path, f"alpha_range={alpha_min}-{alpha_max}")
        else:
            log("REMOVE_REFERENCE_BACKGROUND", reference_image_path)
            image = self.rmbg_worker(image.convert("RGB"))
        stats["time"]["remove background"] = time.time() - start_time

        start_time = time.time()
        generator = self.torch.Generator().manual_seed(int(args.seed))
        outputs = self.i23d_worker(
            image=image,
            num_inference_steps=args.steps,
            guidance_scale=args.guidance_scale,
            generator=generator,
            octree_resolution=args.octree_resolution,
            num_chunks=args.num_chunks,
            output_type="mesh",
        )
        stats["time"]["shape generation"] = time.time() - start_time

        start_time = time.time()
        mesh = self.export_to_trimesh(outputs)[0]
        stats["time"]["export to trimesh"] = time.time() - start_time
        stats["number_of_faces"] = int(mesh.faces.shape[0])
        stats["number_of_vertices"] = int(mesh.vertices.shape[0])

        file_out = self.export_mesh(mesh, save_folder, textured=False)

        start_time = time.time()
        mesh = self.face_reduce_worker(mesh)
        white_obj = self.export_mesh(mesh, save_folder, textured=False, file_type="obj")
        stats["time"]["face reduction"] = time.time() - start_time

        start_time = time.time()
        textured_obj = Path(save_folder) / "textured_mesh.obj"
        path_textured = self.tex_pipeline(
            mesh_path=white_obj,
            image_path=image,
            output_mesh_path=str(textured_obj),
            save_glb=False,
        )
        stats["time"]["texture generation"] = time.time() - start_time

        start_time = time.time()
        glb_path_textured = Path(save_folder) / "textured_mesh.glb"
        file_out2 = self.quick_convert_with_obj2gltf(path_textured, glb_path_textured)
        stats["time"]["convert textured OBJ to GLB"] = time.time() - start_time
        stats["time"]["total"] = time.time() - start_time_0
        output = self.build_model_viewer_html(save_folder, textured=True)
        return file_out, file_out2, output, stats, args.seed

    def export(self, file_out, file_out2, args):
        log("LOCAL_EXPORT_START", file_out, file_out2)
        if not file_out:
            raise RuntimeError("local Hunyuan export requires file_out")
        if args.export_texture:
            if not file_out2:
                raise RuntimeError("local Hunyuan textured export requires file_out2")
            mesh = self.trimesh.load(file_out2)
            save_folder = self.gen_save_folder()
            path = self.export_mesh(mesh, save_folder, textured=True, file_type="glb")
            return self.build_model_viewer_html(save_folder, textured=True), path

        mesh = self.trimesh.load(file_out)
        mesh = self.floater_remove_worker(mesh)
        mesh = self.degenerate_face_remove_worker(mesh)
        if args.reduce_face:
            mesh = self.face_reduce_worker(mesh, args.target_face_num)
        save_folder = self.gen_save_folder()
        path = self.export_mesh(mesh, save_folder, textured=False, file_type="glb")
        return self.build_model_viewer_html(save_folder, textured=False), path


def run_hunyuan_generation(client, reference_image_path, args):
    return client.predict(
        {"path": str(reference_image_path)},
        None,
        None,
        None,
        None,
        args.steps,
        args.guidance_scale,
        args.seed,
        args.octree_resolution,
        True,
        args.num_chunks,
        False,
        api_name="/generation_all",
    )


def run_hunyuan_export(client, file_out, file_out2, args):
    return client.predict(
        extract_file_path(file_out),
        extract_file_path(file_out2),
        "glb",
        args.reduce_face,
        args.export_texture,
        args.target_face_num,
        api_name="/on_export_click",
    )


def run_hunyuan_generation_backend(backend, reference_image_path, args):
    if isinstance(backend, LocalHunyuanBackend):
        return backend.generate(reference_image_path, args)
    return run_hunyuan_generation(backend, reference_image_path, args)


def run_hunyuan_export_backend(backend, file_out, file_out2, args):
    if isinstance(backend, LocalHunyuanBackend):
        return backend.export(extract_file_path(file_out), extract_file_path(file_out2), args)
    return run_hunyuan_export(backend, file_out, file_out2, args)


def copy_glb(download_path, expected_glb_path):
    source = Path(download_path)
    if not source.exists():
        raise RuntimeError(f"Hunyuan export download path does not exist: {source}")
    expected_glb_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, expected_glb_path)
    if not is_valid_glb(expected_glb_path):
        raise RuntimeError(f"exported GLB is missing or too small: {expected_glb_path}")


def extract_export_download_path(export_result):
    if isinstance(export_result, (list, tuple)) and len(export_result) >= 2:
        return extract_file_path(export_result[1])
    return extract_file_path(export_result)


def new_manifest(args):
    return {
        "server": args.server,
        "hunyuan_backend": args.hunyuan_backend,
        "hunyuan_repo": args.hunyuan_repo,
        "model_path": args.model_path,
        "subfolder": args.subfolder,
        "texgen_model_path": args.texgen_model_path,
        "asset_plan": str(Path(args.asset_plan)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "t2i_backend": args.t2i_backend,
        "t2i_model": DASHSCOPE_IMAGE_MODEL,
        "t2i_size": DASHSCOPE_IMAGE_SIZE,
        "reuse_reference_images": args.reuse_reference_images,
        "reference_images_only": args.reference_images_only,
        "assets": [],
    }


def manifest_entry(asset, reference_image_path, expected_glb_path):
    return {
        "asset_id": asset.get("asset_id"),
        "asset_name": asset.get("asset_name"),
        "prompt": asset.get("generation_prompt_en"),
        "negative_prompt": asset.get("negative_prompt_en"),
        "reference_image_path": str(reference_image_path),
        "expected_glb_path": str(expected_glb_path),
        "status": "pending",
        "hunyuan_file_out": None,
        "hunyuan_file_out2": None,
        "export_download_path": None,
        "mesh_stats": {},
        "glb_base_check": None,
        "seed": None,
        "error": None,
    }


def write_manifest(manifest):
    write_json(MANIFEST_PATH, manifest)


def process_asset(backend, asset, args, manifest):
    asset_id = sanitize_filename(asset["asset_id"])
    asset_name = sanitize_filename(asset["asset_name"])
    reference_image_path = REFERENCE_IMAGE_DIR / f"{asset_id}_{asset_name}.png"
    expected_glb_path = resolve_project_path(asset["expected_glb_path"])
    entry = manifest_entry(asset, reference_image_path, expected_glb_path)
    manifest["assets"].append(entry)

    if args.skip_existing and is_valid_glb(expected_glb_path):
        try:
            entry["glb_base_check"] = enforce_glb_base_check(expected_glb_path, asset)
        except Exception as exc:
            log("EXISTING_GLB_BASE_CHECK_FAILED", asset["asset_id"], f"{type(exc).__name__}: {exc}")
        else:
            entry["status"] = "skipped"
            entry["error"] = None
            write_manifest(manifest)
            log("SKIPPED_EXISTING", asset["asset_id"], expected_glb_path)
            return

    try:
        if args.reuse_reference_images and is_valid_reference_image(reference_image_path):
            log("REUSE_REFERENCE_IMAGE", asset["asset_id"], reference_image_path)
            image_result = {"source": "existing_file", "path": str(reference_image_path)}
        else:
            log("GENERATE_REFERENCE_IMAGE", asset["asset_id"], reference_image_path)
            image_result = generate_reference_image(asset, reference_image_path, args.t2i_backend)
        entry["t2i_result"] = image_result
        write_manifest(manifest)

        if args.reference_images_only:
            entry["status"] = "reference_ready"
            entry["error"] = None
            write_manifest(manifest)
            log("REFERENCE_IMAGE_READY", asset["asset_id"], reference_image_path)
            return

        log("HUNYUAN_GENERATION_START", asset["asset_id"])
        generation_result = run_hunyuan_generation_backend(backend, reference_image_path, args)
        raw_generation_path = HUNYUAN_RAW_DIR / f"{asset_id}_generation_all_result.json"
        write_json(raw_generation_path, {"result": repr(generation_result)})

        if not isinstance(generation_result, (list, tuple)) or len(generation_result) < 5:
            raise RuntimeError(f"unexpected /generation_all result: {generation_result}")
        file_out, file_out2, output, mesh_stats, seed = generation_result[:5]
        entry["hunyuan_file_out"] = extract_file_path(file_out)
        entry["hunyuan_file_out2"] = extract_file_path(file_out2)
        entry["mesh_stats"] = mesh_stats if isinstance(mesh_stats, dict) else {"raw": mesh_stats}
        entry["seed"] = seed
        write_manifest(manifest)

        log("HUNYUAN_EXPORT_START", asset["asset_id"])
        export_result = run_hunyuan_export_backend(backend, file_out, file_out2, args)
        raw_export_path = HUNYUAN_RAW_DIR / f"{asset_id}_export_result.json"
        write_json(raw_export_path, {"result": repr(export_result)})
        download_path = extract_export_download_path(export_result)
        if not download_path:
            raise RuntimeError(f"could not extract GLB download path from /on_export_click result: {export_result}")
        entry["export_download_path"] = download_path

        entry["glb_base_check"] = enforce_glb_base_check(download_path, asset)
        copy_glb(download_path, expected_glb_path)
        entry["status"] = "success"
        entry["error"] = None
        write_manifest(manifest)
        log("ASSET_SUCCESS", asset["asset_id"], expected_glb_path)
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["traceback"] = traceback.format_exc()
        write_manifest(manifest)
        log("ASSET_FAILED", asset["asset_id"], entry["error"])
        log_block(entry["traceback"])


def main():
    args = parse_args()
    set_safe_cache_env()
    ensure_dirs()
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("")

    if args.t2i_backend == "auto":
        args.t2i_backend = DEFAULT_T2I_BACKEND
    if args.t2i_backend == "qwen_image":
        args.t2i_backend = DEFAULT_T2I_BACKEND
    if args.t2i_backend not in {
        "dashscope_sdk_qwen_image",
        "dashscope_native",
        "dashscope_qwen_image",
    }:
        raise ValueError(
            "unsupported --t2i_backend. Use dashscope_sdk_qwen_image or dashscope_native."
        )

    asset_plan_path = resolve_project_path(args.asset_plan)
    asset_plan = load_json(asset_plan_path)
    assets = load_assets(asset_plan)
    selected_assets = assets[args.start:args.start + args.limit]

    log("ASSET_PLAN", asset_plan_path)
    log("HUNYUAN_BACKEND", args.hunyuan_backend)
    if args.reference_images_only:
        client_or_backend = None
        log("REFERENCE_IMAGES_ONLY", True)
    elif args.hunyuan_backend == "gradio":
        log("SERVER", args.server)
    else:
        log("LOCAL_HUNYUAN_REPO", args.hunyuan_repo)
        log("LOCAL_MODEL_PATH", args.model_path)
        log("LOCAL_TEXGEN_MODEL_PATH", args.texgen_model_path)
        log("CUDA_VISIBLE_DEVICES", os.environ.get("CUDA_VISIBLE_DEVICES"))
    log("T2I_BACKEND", args.t2i_backend)
    log("T2I_MODEL", DASHSCOPE_IMAGE_MODEL)
    log("REFERENCE_IMAGE_DIR", REFERENCE_IMAGE_DIR)
    log("ASSET_COUNT", len(selected_assets))

    if args.reference_images_only:
        client_or_backend = None
    elif args.hunyuan_backend == "gradio":
        try:
            from gradio_client import Client
        except Exception as exc:
            raise RuntimeError(f"gradio_client is required for Hunyuan3D API calls: {exc}") from exc
        client_or_backend = Client(args.server)
    else:
        client_or_backend = LocalHunyuanBackend(args)

    manifest = new_manifest(args)
    manifest["asset_plan"] = str(asset_plan_path)
    write_manifest(manifest)

    for asset in selected_assets:
        process_asset(client_or_backend, asset, args, manifest)
        time.sleep(0.2)

    success_count = sum(1 for item in manifest["assets"] if item["status"] == "success")
    reference_count = sum(1 for item in manifest["assets"] if item["status"] == "reference_ready")
    skipped_count = sum(1 for item in manifest["assets"] if item["status"] == "skipped")
    failed_count = sum(1 for item in manifest["assets"] if item["status"] == "failed")
    log("MANIFEST_OUTPUT", MANIFEST_PATH)
    log("SUMMARY", f"success={success_count}", f"reference_ready={reference_count}", f"skipped={skipped_count}", f"failed={failed_count}")
    if failed_count:
        raise RuntimeError(f"asset generation failed for {failed_count} asset(s); see log and manifest for full traceback")
    log("DONE")


if __name__ == "__main__":
    main()
