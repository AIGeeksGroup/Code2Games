'use strict';

(() => {
  const videos = [...document.querySelectorAll('.player video')];
  const visible = new Set();
  const userPaused = new WeakSet();
  const automaticPauses = new WeakSet();

  function loadVideo(video) {
    if (!video.dataset.src) return;
    video.src = video.dataset.src;
    delete video.dataset.src;
    video.load();
  }

  function pauseVideo(video) {
    if (video.paused) return;
    automaticPauses.add(video);
    video.pause();
  }

  function playVideo(video) {
    if (document.hidden || userPaused.has(video)) return;
    loadVideo(video);
    video.muted = true;
    const attempt = video.play();
    if (attempt) attempt.catch((error) => {
      if (error.name !== 'AbortError' && visible.has(video)) {
        video.closest('.player').querySelector('.play-fallback').hidden = false;
      }
    });
  }

  for (const video of videos) {
    video.muted = true;
    video.defaultMuted = true;
    const player = video.closest('.player');
    const fallback = player.querySelector('.play-fallback');
    video.addEventListener('playing', () => { fallback.hidden = true; });
    video.addEventListener('play', () => { userPaused.delete(video); });
    video.addEventListener('pause', () => {
      if (automaticPauses.has(video)) {
        automaticPauses.delete(video);
      } else if (visible.has(video) && !document.hidden && !video.ended) {
        userPaused.add(video);
      }
    });
    video.addEventListener('error', () => {
      fallback.hidden = true;
      player.querySelector('.video-error').hidden = false;
    });
    fallback.addEventListener('click', () => {
      userPaused.delete(video);
      playVideo(video);
    });
  }

  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const video = entry.target;
        if (entry.isIntersecting) {
          visible.add(video);
          playVideo(video);
        } else {
          visible.delete(video);
          pauseVideo(video);
        }
      }
    }, { threshold: 0.06, rootMargin: '80px 0px' });
    videos.forEach((video) => observer.observe(video));
  } else {
    videos.forEach((video) => { visible.add(video); playVideo(video); });
  }

  document.addEventListener('visibilitychange', () => {
    videos.forEach((video) => {
      if (document.hidden) pauseVideo(video);
      else if (visible.has(video)) playVideo(video);
    });
  });

  document.getElementById('game-jump').addEventListener('change', (event) => {
    const target = document.getElementById(event.target.value);
    if (!target) return;
    target.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
    target.querySelector('h2').focus({ preventScroll: true });
  });

  document.querySelectorAll('.expand-video').forEach((button) => {
    button.addEventListener('click', async () => {
      const video = document.getElementById(button.dataset.video);
      loadVideo(video);
      try {
        if (video.requestFullscreen) await video.requestFullscreen();
        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
        else window.open(video.currentSrc || video.src, '_blank', 'noopener');
      } catch {
        window.open(video.currentSrc || video.src, '_blank', 'noopener');
      }
    });
  });
})();
