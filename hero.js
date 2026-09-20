'use strict';

(() => {
  const hero = document.querySelector('.hero');
  const header = document.querySelector('.site-header');
  const video = hero.querySelector('.hero-background');
  const trigger = hero.querySelector('.hero-trigger');
  const playback = hero.querySelector('.hero-playback');
  const sceneName = document.getElementById('hero-scene-name');
  const sceneIndex = document.getElementById('hero-scene-index');
  const scenes = [
    'Underwater Exploration', 'Monster Hunt', 'Third-Person Shooter',
    'Desert Rally', 'First-Person Shooter', 'Block World',
    'Temple Adventure', 'Alpine Skiing', 'Flight Missions', 'Archer Hunt',
  ];
  const sceneDuration = 6;
  let pinned = false;
  let userPaused = false;
  let inView = true;
  let pendingSeek = null;

  function updateHeaderStyle() {
    const scrollInset = parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop) || header.offsetHeight;
    header.classList.toggle('is-scrolled', hero.getBoundingClientRect().bottom <= scrollInset + 1);
  }

  window.addEventListener('scroll', updateHeaderStyle, { passive: true });
  window.addEventListener('resize', updateHeaderStyle);
  window.addEventListener('pageshow', updateHeaderStyle);
  updateHeaderStyle();

  function setExpanded(expanded) {
    hero.classList.toggle('is-expanded', expanded);
    trigger.setAttribute('aria-expanded', String(expanded));
    trigger.setAttribute('aria-label', expanded ? 'Collapse game preview' : 'Expand game preview');
  }

  trigger.addEventListener('pointerenter', (event) => {
    if (event.pointerType === 'mouse') setExpanded(true);
  });
  trigger.addEventListener('pointerleave', () => { if (!pinned) setExpanded(false); });
  trigger.addEventListener('focus', () => setExpanded(true));
  trigger.addEventListener('blur', () => { if (!pinned) setExpanded(false); });
  trigger.addEventListener('click', () => { pinned = !pinned; setExpanded(pinned); });
  hero.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { pinned = false; setExpanded(false); }
  });

  function updatePlaybackButton() {
    const paused = video.paused;
    playback.classList.toggle('is-paused', paused);
    playback.setAttribute('aria-label', paused ? 'Play background video' : 'Pause background video');
    playback.title = paused ? 'Play background video' : 'Pause background video';
  }

  function play() {
    if (userPaused || !inView || document.hidden) return;
    video.muted = true;
    video.play().catch(updatePlaybackButton);
  }

  playback.addEventListener('click', () => {
    userPaused = !video.paused;
    if (userPaused) video.pause();
    else play();
  });
  video.addEventListener('play', updatePlaybackButton);
  video.addEventListener('pause', updatePlaybackButton);
  video.addEventListener('error', () => { playback.hidden = true; });

  const buttons = scenes.map((name, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'hero-scene-button';
    button.setAttribute('aria-label', `Show ${name}`);
    button.title = name;
    button.addEventListener('click', () => {
      if (video.readyState >= 1) video.currentTime = index * sceneDuration;
      else pendingSeek = index * sceneDuration;
      updateScene(index * sceneDuration);
      play();
    });
    hero.querySelector('.hero-scenes').append(button);
    return button;
  });

  function updateScene(time = video.currentTime) {
    const index = Math.min(scenes.length - 1, Math.floor(time / sceneDuration));
    sceneName.textContent = scenes[index];
    sceneIndex.textContent = String(index + 1).padStart(2, '0');
    buttons.forEach((button, i) => {
      button.setAttribute('aria-current', String(i === index));
      button.style.setProperty('--scene-progress', i === index ? `${Math.max(3, (time % sceneDuration) / sceneDuration * 100)}%` : '0%');
    });
  }
  video.addEventListener('timeupdate', () => updateScene());
  video.addEventListener('loadedmetadata', () => {
    if (pendingSeek !== null) { video.currentTime = pendingSeek; pendingSeek = null; }
    play();
  });

  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(([entry]) => {
      inView = entry.isIntersecting && entry.intersectionRatio > 0.15;
      if (inView) play();
      else video.pause();
    }, { threshold: 0.15 });
    observer.observe(hero);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) video.pause();
    else play();
  });

  video.muted = true;
  video.defaultMuted = true;
  updateScene();
  play();
})();
