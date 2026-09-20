# Code2Games

**Enabling Coding Agents for Gaming World Generation**

Open `index.html` directly in a browser, or serve this directory with `python3 -m http.server 8000`.

## Opening screen

A viewport-height opening screen plays a muted, looping reel with a six-second excerpt from each of the ten games. Hover over the upper-left preview to move the diagonal divider down and reveal more of the scene; tap to toggle on touchscreens. The background can be paused, and the ten markers switch between games. Scroll down to view the complete recordings.

## Showcase

Ten game categories, each with two local video examples. Detailed underwater, monster-hunt, and tactical scenes lead the collection. Video captions follow the scenarios in `video/game_prompts.txt`.

- Videos keep their full 16:9 image, including the game interface, with no cropping.
- Desktop layouts show each pair side by side. Smaller screens stack the videos.
- Videos play muted and loop automatically as they enter view. Off-screen videos pause to keep scrolling responsive; user-initiated pauses are respected.
- Native video controls, fullscreen buttons, and a game selector support browsing.
- Fonts and preview images are local, so the page has no external runtime dependencies.

## Files

- `index.html` — project title and twenty video examples.
- `styles.css` — responsive presentation.
- `app.js` — video playback, section navigation, and fullscreen controls.
- `hero.css` and `hero.js` — the opening screen, diagonal interaction, and background controls.
- `assets/hero-reel.mp4` — a 60-second background reel assembled from the supplied recordings.
- `assets/posters/` — preview frames extracted from the supplied videos.
- `assets/fonts/` — Nunito and its SIL Open Font License.
- `video/` — original recordings and prompts.

## Design reference

The dark background, cyan accents, rounded typography, and fine curved lines take inspiration from [TiMi Studio Group](https://www.timistudios.com/), adapted to a compact project page. The page uses the open-source [Nunito font](https://github.com/googlefonts/nunito); its license is included in `assets/fonts/Nunito-OFL.txt`.
