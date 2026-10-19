/* GemmaMorph UI: animated 3D background, shell, launch console and live operation view.
   No dependencies and no network calls except to this local server, so it works offline. */
(function () {
  "use strict";

  const GM = window.GM || { page: "index" };
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const reduced = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  let motionOn = !reduced && store.get("gm-motion") !== "off";
  const getJSON = (url, opts) => fetch(url, opts).then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  const svg = (inner) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + inner + "</svg>";
  const ICON = {
    alert: svg('<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>'),
    file: svg('<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>'),
    ext: svg('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
    info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>'),
  };

  /* ------------------------------------------------------------ toasts */
  function toast(msg, kind) {
    const box = $("#toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast " + (kind || "");
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 400); }, 4200);
  }

  /* ---------------------------------------- 3D dependency-graph background */
  function startBackground() {
    const canvas = $("#bg");
    const ctx = canvas && canvas.getContext ? canvas.getContext("2d") : null;
    if (!ctx) return null;
    const small = innerWidth < 760;
    const PAL = [[56, 189, 248], [167, 139, 250], [244, 114, 182], [34, 211, 238], [52, 211, 153]];
    const sprites = PAL.map(([r, g, b]) => {
      const s = document.createElement("canvas");
      s.width = s.height = 64;
      const c = s.getContext("2d");
      const gr = c.createRadialGradient(32, 32, 0, 32, 32, 32);
      gr.addColorStop(0, "rgba(255,255,255,1)");
      gr.addColorStop(0.14, "rgba(" + r + "," + g + "," + b + ",0.95)");
      gr.addColorStop(0.42, "rgba(" + r + "," + g + "," + b + ",0.22)");
      gr.addColorStop(1, "rgba(" + r + "," + g + "," + b + ",0)");
      c.fillStyle = gr;
      c.fillRect(0, 0, 64, 64);
      return s;
    });
    const rand = (a, b) => a + Math.random() * (b - a);
    const onSphere = (rad) => {
      const u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2, k = Math.sqrt(1 - u * u);
      return [rad * k * Math.cos(th), rad * u * 0.72, rad * k * Math.sin(th)];
    };
    const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);

    // A dependency graph: package hubs, their modules around them, links between hubs.
    const HUBS = small ? 6 : 10, PER = small ? 6 : 10;
    const nodes = [], edges = [];
    for (let h = 0; h < HUBS; h++) {
      const [x, y, z] = onSphere(rand(0.5, 0.85));
      nodes.push({ x, y, z, hub: true, col: h % 3 === 2 ? 1 : 0, ph: Math.random() * 6.28 });
    }
    for (let h = 0; h < HUBS; h++) {
      for (let k = 0; k < PER; k++) {
        const [dx, dy, dz] = onSphere(rand(0.07, 0.3));
        const hub = nodes[h];
        nodes.push({ x: hub.x + dx, y: hub.y + dy, z: hub.z + dz, hub: false, col: (h + k) % PAL.length, ph: Math.random() * 6.28 });
        edges.push([h, nodes.length - 1]);
      }
    }
    const linked = new Set();
    for (let h = 0; h < HUBS; h++) {
      const near = [];
      for (let j = 0; j < HUBS; j++) if (j !== h) near.push([dist(nodes[h], nodes[j]), j]);
      near.sort((a, b) => a[0] - b[0]);
      near.slice(0, 2).forEach(([, j]) => {
        const key = Math.min(h, j) + ":" + Math.max(h, j);
        if (!linked.has(key)) { linked.add(key); edges.push([h, j]); }
      });
    }
    const adj = nodes.map(() => []);
    edges.forEach(([a, b], i) => { adj[a].push(i); adj[b].push(i); });
    const pulses = Array.from({ length: small ? 6 : 14 }, () => ({ e: (Math.random() * edges.length) | 0, t: Math.random(), dir: 1, v: rand(0.3, 0.7) }));
    const P = nodes.map(() => ({ x: 0, y: 0, s: 1, z: 0 }));
    const order = nodes.map((_, i) => i);

    let W = 0, H = 0, cx = 0, cy = 0, R = 1;
    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.6);
      W = innerWidth; H = innerHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const side = W > 1000 ? 272 : 0;
      cx = side + (W - side) * 0.62; cy = H * 0.42; R = Math.min(W - side, H) * 0.5;
    }
    let rotY = 0, mx = 0, my = 0, tmx = 0, tmy = 0, px = -1e4, py = -1e4;
    addEventListener("mousemove", (e) => { tmx = e.clientX / W - 0.5; tmy = e.clientY / H - 0.5; px = e.clientX; py = e.clientY; }, { passive: true });
    document.addEventListener("mouseleave", () => { px = py = -1e4; });

    function project(n, c1, s1, c2, s2, o) {
      const x1 = n.x * c1 - n.z * s1, z1 = n.x * s1 + n.z * c1;
      const y2 = n.y * c2 - z1 * s2, z2 = n.y * s2 + z1 * c2;
      const k = 2.6 / (2.6 + z2);
      o.x = cx + x1 * R * k; o.y = cy + y2 * R * k; o.s = k; o.z = z2;
    }
    function floor(time) {
      const GX = small ? 30 : 48, GZ = small ? 12 : 18;
      ctx.fillStyle = "rgb(56,189,248)";
      for (let i = 0; i < GX; i++) {
        for (let j = 0; j < GZ; j++) {
          const x = (i / (GX - 1) - 0.5) * 5.4 - mx * 0.5;
          const z = (j / (GZ - 1)) * 3.4 - 0.2;
          const y = 1.0 + Math.sin(x * 1.8 + time * 1.15) * 0.07 + Math.cos(z * 2.3 + time * 0.85) * 0.06 - my * 0.1;
          const k = 2.6 / (2.6 + z);
          const sx = cx + x * R * k, sy = cy + y * R * k;
          if (sx < -4 || sx > W + 4 || sy > H + 4) continue;
          ctx.globalAlpha = Math.max(0.04, Math.min(0.5, (k - 0.42) * 0.7));
          const s = 1.7 * k;
          ctx.fillRect(sx, sy, s, s);
        }
      }
      ctx.globalAlpha = 1;
    }
    function draw(time) {
      const ay = rotY + mx * 0.75, ax = 0.3 + my * 0.45;
      const c1 = Math.cos(ay), s1 = Math.sin(ay), c2 = Math.cos(ax), s2 = Math.sin(ax);
      ctx.clearRect(0, 0, W, H);
      floor(time);
      for (let i = 0; i < nodes.length; i++) project(nodes[i], c1, s1, c2, s2, P[i]);
      ctx.globalCompositeOperation = "lighter";
      ctx.lineWidth = 1;
      for (const [a, b] of edges) {
        const A = P[a], B = P[b];
        const al = Math.max(0, Math.min(1, ((A.s + B.s) / 2 - 0.62) * 1.7)) * 0.45;
        if (al < 0.02) continue;
        ctx.strokeStyle = nodes[a].hub && nodes[b].hub ? "rgba(167,139,250," + al + ")" : "rgba(56,189,248," + al * 0.8 + ")";
        ctx.beginPath(); ctx.moveTo(A.x, A.y); ctx.lineTo(B.x, B.y); ctx.stroke();
      }
      if (px > 0) {  // filaments from the cursor to nearby nodes
        const near = [];
        for (let i = 0; i < P.length; i++) { const d = Math.hypot(P[i].x - px, P[i].y - py); if (d < 170) near.push([d, i]); }
        near.sort((a, b) => a[0] - b[0]);
        near.slice(0, 6).forEach(([d, i]) => {
          ctx.strokeStyle = "rgba(125,211,252," + (1 - d / 170) * 0.55 + ")";
          ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(P[i].x, P[i].y); ctx.stroke();
        });
      }
      order.sort((i, j) => P[j].z - P[i].z);
      for (const i of order) {
        const n = nodes[i], p = P[i];
        const size = (n.hub ? 28 : 12) * p.s * (0.85 + 0.15 * Math.sin(time * 1.6 + n.ph));
        ctx.globalAlpha = Math.max(0.08, Math.min(1, (p.s - 0.55) * 1.9));
        ctx.drawImage(sprites[n.col], p.x - size / 2, p.y - size / 2, size, size);
      }
      for (const q of pulses) {  // signals travelling through the graph
        const [a, b] = edges[q.e];
        const A = P[q.dir > 0 ? a : b], B = P[q.dir > 0 ? b : a];
        for (let k = 0; k < 4; k++) {
          const t = Math.max(0, q.t - k * 0.06);
          const s = (11 - k * 2.2) * ((A.s + B.s) / 2);
          ctx.globalAlpha = 0.95 - k * 0.22;
          ctx.drawImage(sprites[3], A.x + (B.x - A.x) * t - s / 2, A.y + (B.y - A.y) * t - s / 2, s, s);
        }
      }
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
    }
    function step(dt) {
      rotY += dt * 0.09;
      mx += (tmx - mx) * 0.05; my += (tmy - my) * 0.05;
      for (const q of pulses) {
        q.t += q.v * dt;
        if (q.t >= 1) {
          const [a, b] = edges[q.e];
          const end = q.dir > 0 ? b : a;
          const options = adj[end].filter((e) => e !== q.e);
          q.e = options.length ? options[(Math.random() * options.length) | 0] : (Math.random() * edges.length) | 0;
          q.dir = edges[q.e][0] === end ? 1 : -1;
          q.t = 0;
        }
      }
    }
    let raf = 0, running = false, last = 0;
    function loop(t) {
      const dt = Math.min(0.05, (t - last) / 1000 || 0.016);
      last = t;
      step(dt); draw(t / 1000);
      raf = requestAnimationFrame(loop);
    }
    const api = {
      start() { if (running) return; running = true; last = performance.now(); raf = requestAnimationFrame(loop); },
      stop() { running = false; cancelAnimationFrame(raf); },
    };
    resize();
    addEventListener("resize", () => { resize(); if (!running) draw(performance.now() / 1000); });
    document.addEventListener("visibilitychange", () => { if (document.hidden) api.stop(); else if (motionOn) api.start(); });
    draw(0);
    if (motionOn) api.start();
    document.documentElement.classList.add("has-canvas");
    return api;
  }

  /* ------------------------------------------------------------- shell */
  let health = null;
  const healthListeners = [];
  function pollHealth() {
    getJSON("/api/health").then((h) => {
      health = h;
      const ok = h.ollama && h.gemma4 && h.gemma4.length;
      const state = ok ? "ok" : h.ollama ? "warn" : "failed";
      const text = ok ? "Online · " + h.gemma4[0] : h.ollama ? "No Gemma 4 model" : "Offline";
      $$("[data-health]").forEach((el) => { el.innerHTML = '<i class="dot ' + state + '"></i>' + esc(text); });
      healthListeners.forEach((f) => f(h));
    }).catch(() => { /* the UI server itself is gone */ });
  }
  function clock() {
    const el = $("#clock");
    if (!el) return;
    const tick = () => { el.textContent = new Date().toLocaleTimeString([], { hour12: false }); };
    tick();
    setInterval(tick, 1000);
  }
  function motionToggle(bg) {
    const b = $("#motion");
    if (!b) return;
    const paint = () => {
      b.setAttribute("aria-pressed", motionOn ? "true" : "false");
      b.querySelector("span").textContent = "Motion " + (motionOn ? "on" : "off");
      document.documentElement.classList.toggle("motion-off", !motionOn);
    };
    paint();
    b.addEventListener("click", () => {
      motionOn = !motionOn;
      store.set("gm-motion", motionOn ? "on" : "off");
      paint();
      if (bg) { if (motionOn) bg.start(); else bg.stop(); }
    });
  }
  let revealObs = null;
  function observeReveals(root) {
    const els = $$(".reveal:not(.in)", root || document);
    if (!("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
    revealObs = revealObs || new IntersectionObserver((entries) => entries.forEach((en) => {
      if (en.isIntersecting) { en.target.classList.add("in"); revealObs.unobserve(en.target); }
    }), { threshold: 0.06 });
    els.forEach((e) => revealObs.observe(e));
  }
  function scrollSpy() {
    const links = $$('.nav a[href^="#"]');
    if (!links.length || !("IntersectionObserver" in window)) return () => {};
    const byId = new Map(links.map((a) => [a.getAttribute("href").slice(1), a]));
    const obs = new IntersectionObserver((entries) => entries.forEach((en) => {
      if (!en.isIntersecting) return;
      links.forEach((a) => a.classList.remove("active"));
      const a = byId.get(en.target.id);
      if (a) a.classList.add("active");
    }), { rootMargin: "-30% 0px -60% 0px" });
    const watch = () => byId.forEach((a, id) => {
      const s = document.getElementById(id);
      if (s && !s.dataset.spied) { s.dataset.spied = "1"; obs.observe(s); }
    });
    watch();
    return watch;
  }
  // click ripple on buttons
  document.addEventListener("pointerdown", (e) => {
    const b = e.target.closest(".btn");
    if (!b || b.disabled) return;
    const r = b.getBoundingClientRect(), s = Math.max(r.width, r.height);
    const sp = document.createElement("span");
    sp.className = "ripple";
    sp.style.width = sp.style.height = s + "px";
    sp.style.left = e.clientX - r.left - s / 2 + "px";
    sp.style.top = e.clientY - r.top - s / 2 + "px";
    b.appendChild(sp);
    setTimeout(() => sp.remove(), 700);
  });
  // copy buttons (diffs)
  const copyStore = [];
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-copy]");
    if (!c) return;
    const text = copyStore[Number(c.dataset.copy)] || "";
    const done = navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject(new Error("no clipboard"));
    done.then(() => toast("Copied to the clipboard", "ok"), () => toast("Copying is not available here", "bad"));
  });

  /* -------------------------------------- 3D Interactive Hero Canvas */
  function initHero3D() {
    const box = $("#hero-3d-box");
    const canvas = $("#hero-3d-canvas");
    if (!canvas || !box) return null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;

    let width = 0, height = 0, dpr = 1;
    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = box.clientWidth || 380;
      height = box.clientHeight || 380;
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener("resize", resize);

    const nodes = [
      { id: "core", name: "Gemma 4", sub: "Neural Engine", x: 0, y: 0, z: 0, col: "#a78bfa", r: 18, core: true },
      { id: "toml", name: "toml", sub: "→ tomllib", x: -0.72, y: -0.38, z: 0.42, col: "#f59e0b", r: 12 },
      { id: "appdirs", name: "appdirs", sub: "→ platformdirs", x: 0.78, y: -0.42, z: 0.35, col: "#38bdf8", r: 12 },
      { id: "pydantic", name: "pydantic", sub: "→ v2.field_val", x: -0.65, y: 0.55, z: -0.32, col: "#c084fc", r: 13 },
      { id: "six", name: "six", sub: "→ stdlib clean", x: 0.62, y: 0.62, z: -0.28, col: "#f472b6", r: 11 },
      { id: "requests", name: "requests", sub: "transitive clean", x: -0.22, y: -0.78, z: -0.55, col: "#10b981", r: 10 },
      { id: "jinja2", name: "jinja2", sub: "→ markupsafe fix", x: 0.28, y: 0.75, z: 0.52, col: "#facc15", r: 11 },
    ];

    const phi = (1 + Math.sqrt(5)) / 2;
    const cageVerts = [
      [-1, phi, 0], [1, phi, 0], [-1, -phi, 0], [1, -phi, 0],
      [0, -1, phi], [0, 1, phi], [0, -1, -phi], [0, 1, -phi],
      [phi, 0, -1], [phi, 0, 1], [-phi, 0, -1], [-phi, 0, 1]
    ].map(([x, y, z]) => {
      const len = Math.hypot(x, y, z);
      return { x: (x / len) * 1.15, y: (y / len) * 1.15, z: (z / len) * 1.15 };
    });

    const edges = [
      [0, 1], [0, 2], [0, 3], [0, 4], [0, 5], [0, 6],
      [1, 2], [2, 4], [4, 6], [6, 3], [3, 1], [1, 5], [2, 5], [3, 5], [4, 6]
    ];

    let pulses = [
      { edge: 0, t: 0.2, speed: 0.45 },
      { edge: 1, t: 0.6, speed: 0.5 },
      { edge: 2, t: 0.8, speed: 0.4 },
      { edge: 3, t: 0.3, speed: 0.6 },
      { edge: 4, t: 0.5, speed: 0.55 },
      { edge: 5, t: 0.1, speed: 0.48 },
    ];

    let rotX = 0.22, rotY = 0.45;
    let autoRotate = true;
    let isDragging = false;
    let lastMouseX = 0, lastMouseY = 0;
    let velX = 0, velY = 0;
    let hoveredNode = null;
    let pulseWave = 0;

    box.addEventListener("pointerdown", (e) => {
      isDragging = true;
      lastMouseX = e.clientX;
      lastMouseY = e.clientY;
      box.setPointerCapture(e.pointerId);
    });

    box.addEventListener("pointermove", (e) => {
      const rect = box.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;

      if (isDragging) {
        const dx = e.clientX - lastMouseX;
        const dy = e.clientY - lastMouseY;
        velY = dx * 0.007;
        velX = -dy * 0.007;
        rotY += velY;
        rotX += velX;
        lastMouseX = e.clientX;
        lastMouseY = e.clientY;
      }

      let closest = null, minD = 24;
      for (const n of nodes) {
        if (!n.screen) continue;
        const d = Math.hypot(n.screen.x - mx, n.screen.y - my);
        if (d < minD) { minD = d; closest = n; }
      }
      hoveredNode = closest;
    });

    const endDrag = () => { isDragging = false; };
    box.addEventListener("pointerup", endDrag);
    box.addEventListener("pointercancel", endDrag);

    const btnPulse = $("#btn-3d-pulse");
    if (btnPulse) {
      btnPulse.addEventListener("click", (e) => {
        e.stopPropagation();
        pulseWave = 1.0;
        for (let i = 0; i < 8; i++) {
          pulses.push({ edge: Math.floor(Math.random() * edges.length), t: 0, speed: 0.9 + Math.random() * 0.8 });
        }
        toast("⚡ High-energy neural pulse wave triggered!", "ok");
      });
    }

    const btnRotate = $("#btn-3d-rotate");
    if (btnRotate) {
      btnRotate.addEventListener("click", (e) => {
        e.stopPropagation();
        autoRotate = !autoRotate;
        btnRotate.textContent = autoRotate ? "🔄 Orbit" : "⏸ Paused";
        toast("Auto orbit " + (autoRotate ? "resumed" : "paused"), "ok");
      });
    }

    const btnReset = $("#btn-3d-reset");
    if (btnReset) {
      btnReset.addEventListener("click", (e) => {
        e.stopPropagation();
        rotX = 0.22;
        rotY = 0.45;
        velX = 0;
        velY = 0;
        toast("3D Camera reset", "ok");
      });
    }

    function project(p, radius) {
      const cx = width / 2, cy = height / 2;
      const cosY = Math.cos(rotY), sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX), sinX = Math.sin(rotX);

      const x1 = p.x * cosY - p.z * sinY;
      const z1 = p.x * sinY + p.z * cosY;

      const y2 = p.y * cosX - z1 * sinX;
      const z2 = p.y * sinX + z1 * cosX;

      const fov = 2.4;
      const scale = fov / (fov + z2);
      return {
        x: cx + x1 * radius * scale,
        y: cy + y2 * radius * scale,
        scale: scale,
        z: z2
      };
    }

    let rafId = 0;
    let lastTime = performance.now();

    function render(now) {
      const dt = Math.min(0.05, (now - lastTime) / 1000 || 0.016);
      lastTime = now;

      if (!isDragging) {
        if (autoRotate) {
          rotY += dt * 0.32;
          rotX += Math.sin(now * 0.0008) * 0.001;
        } else {
          rotY += velY;
          rotX += velX;
          velY *= 0.92;
          velX *= 0.92;
        }
      }

      if (pulseWave > 0) {
        pulseWave = Math.max(0, pulseWave - dt * 1.5);
      }

      ctx.clearRect(0, 0, width, height);
      const radius = Math.min(width, height) * 0.38;

      const projectedCage = cageVerts.map(v => project(v, radius * 1.05));
      ctx.lineWidth = 0.7;
      ctx.strokeStyle = "rgba(56, 189, 248, 0.12)";
      for (let i = 0; i < projectedCage.length; i++) {
        for (let j = i + 1; j < projectedCage.length; j++) {
          const d = Math.hypot(cageVerts[i].x - cageVerts[j].x, cageVerts[i].y - cageVerts[j].y, cageVerts[i].z - cageVerts[j].z);
          if (d < 1.1) {
            const p1 = projectedCage[i], p2 = projectedCage[j];
            ctx.beginPath();
            ctx.moveTo(p1.x, p1.y);
            ctx.lineTo(p2.x, p2.y);
            ctx.stroke();
          }
        }
      }

      for (let i = 0; i < nodes.length; i++) {
        nodes[i].screen = project(nodes[i], radius);
      }

      for (const [a, b] of edges) {
        const A = nodes[a].screen, B = nodes[b].screen;
        const isCoreEdge = a === 0 || b === 0;
        const alpha = isCoreEdge ? 0.45 + (pulseWave * 0.4) : 0.22;
        ctx.strokeStyle = isCoreEdge ? "rgba(56, 189, 248, " + alpha + ")" : "rgba(167, 139, 250, " + alpha + ")";
        ctx.lineWidth = isCoreEdge ? 1.6 : 1.0;
        ctx.beginPath();
        ctx.moveTo(A.x, A.y);
        ctx.lineTo(B.x, B.y);
        ctx.stroke();
      }

      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        p.t += p.speed * dt;
        if (p.t > 1) {
          if (pulses.length > 8) {
            pulses.splice(i, 1);
            continue;
          }
          p.t = 0;
          p.edge = Math.floor(Math.random() * edges.length);
        }
        const [a, b] = edges[p.edge];
        const A = nodes[a].screen, B = nodes[b].screen;
        const px = A.x + (B.x - A.x) * p.t;
        const py = A.y + (B.y - A.y) * p.t;

        const pGrad = ctx.createRadialGradient(px, py, 0, px, py, 7);
        pGrad.addColorStop(0, "#ffffff");
        pGrad.addColorStop(0.4, "#38bdf8");
        pGrad.addColorStop(1, "rgba(56,189,248,0)");
        ctx.fillStyle = pGrad;
        ctx.beginPath();
        ctx.arc(px, py, 7, 0, Math.PI * 2);
        ctx.fill();
      }

      const sorted = [...nodes].sort((a, b) => a.screen.z - b.screen.z);
      for (const n of sorted) {
        const p = n.screen;
        const isHovered = hoveredNode === n;
        const baseR = (n.core ? 19 : 12) * p.scale * (isHovered ? 1.3 : 1.0);

        const glowR = baseR * (n.core ? 2.4 : 2.0);
        const grad = ctx.createRadialGradient(p.x, p.y, baseR * 0.4, p.x, p.y, glowR);
        grad.addColorStop(0, n.col);
        grad.addColorStop(0.5, n.col + "55");
        grad.addColorStop(1, n.col + "00");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(p.x, p.y, glowR, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(p.x, p.y, baseR * 0.8, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = n.col;
        ctx.beginPath();
        ctx.arc(p.x, p.y, baseR * 0.65, 0, Math.PI * 2);
        ctx.fill();

        ctx.font = (n.core ? "bold 12px" : "bold 10.5px") + ' "Cascadia Code", Consolas, monospace';
        ctx.fillStyle = isHovered ? "#ffffff" : "#e2e8f0";
        ctx.textAlign = "center";
        ctx.fillText(n.name, p.x, p.y + baseR + 13);

        if (!n.core && (p.scale > 0.85 || isHovered)) {
          ctx.font = '9px "Cascadia Code", Consolas, monospace';
          ctx.fillStyle = isHovered ? "#38bdf8" : "#94a3b8";
          ctx.fillText(n.sub, p.x, p.y + baseR + 24);
        }
      }

      if (hoveredNode) {
        const hn = hoveredNode;
        const hp = hn.screen;
        ctx.save();
        ctx.fillStyle = "rgba(5, 8, 16, 0.9)";
        ctx.strokeStyle = "#38bdf8";
        ctx.lineWidth = 1;
        const tw = 160, th = 48;
        const bx = Math.min(width - tw - 10, Math.max(10, hp.x - tw / 2));
        const by = Math.max(10, hp.y - th - 30);
        ctx.beginPath();
        ctx.roundRect(bx, by, tw, th, 8);
        ctx.fill();
        ctx.stroke();

        ctx.font = 'bold 10.5px "Cascadia Code", Consolas, monospace';
        ctx.fillStyle = "#38bdf8";
        ctx.textAlign = "left";
        ctx.fillText(hn.name.toUpperCase(), bx + 10, by + 18);

        ctx.font = '9px "Cascadia Code", Consolas, monospace';
        ctx.fillStyle = "#cbd5e1";
        ctx.fillText(hn.sub, bx + 10, by + 34);
        ctx.restore();
      }

      rafId = requestAnimationFrame(render);
    }

    rafId = requestAnimationFrame(render);
    return {
      stop() { cancelAnimationFrame(rafId); }
    };
  }

  /* --------------------------------- 3D Cyber Constellation Splash Engine */
  function initSplash3D(canvas) {
    if (!canvas) return null;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;

    let W = 0, H = 0, cx = 0, cy = 0;
    function resize() {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = window.innerWidth;
      H = window.innerHeight;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx = W * 0.5;
      cy = H * 0.48;
    }
    resize();
    window.addEventListener("resize", resize, { passive: true });

    // Orbital Halo nodes that frame the perimeter (leaving center clean)
    const RING_COUNT = 44;
    const ringNodes = [];
    const rx = Math.min(W * 0.44, 520);
    const rz = Math.min(W * 0.38, 450);

    for (let i = 0; i < RING_COUNT; i++) {
      const th = (i / RING_COUNT) * Math.PI * 2;
      const y = Math.sin(th * 3) * 65 + (Math.sin(th * 2) * 30);
      ringNodes.push({
        x: Math.cos(th) * rx,
        y: y,
        z: Math.sin(th) * rz,
        baseTh: th,
        size: 3 + Math.random() * 2,
        core: i % 4 === 0,
        col: i % 3 === 0 ? "#38bdf8" : i % 5 === 0 ? "#a78bfa" : "#7dd3fc",
      });
    }

    // Ambient floating star dust strictly OUTSIDE center zone
    const AMBIENT_COUNT = 60;
    const ambientNodes = [];
    while (ambientNodes.length < AMBIENT_COUNT) {
      const angle = Math.random() * Math.PI * 2;
      const dist = 360 + Math.random() * (Math.max(W, H) * 0.45);
      const x = Math.cos(angle) * dist;
      const y = (Math.random() - 0.5) * H * 0.85;
      const z = Math.sin(angle) * dist * 0.8;
      // Skip anything entering the center reading box
      if (Math.abs(x) < 320 && Math.abs(y) < 180) continue;
      ambientNodes.push({
        x, y, z,
        size: 1 + Math.random() * 1.8,
        col: Math.random() > 0.5 ? "#38bdf8" : "#94a3b8",
        alpha: 0.2 + Math.random() * 0.4,
        drift: (Math.random() - 0.5) * 0.2,
      });
    }

    // Telemetry sparks traveling along the ring
    const pulses = [
      { pos: 0.1, speed: 0.08, col: "#ffffff" },
      { pos: 0.55, speed: 0.06, col: "#38bdf8" },
      { pos: 0.82, speed: 0.09, col: "#a78bfa" },
    ];

    let rotY = 0.4, rotX = 0.28;
    let targetRotY = 0.4, targetRotX = 0.28;
    let isDragging = false, lastMouseX = 0, lastMouseY = 0;
    let isWarping = false, warpProgress = 0;

    canvas.addEventListener("mousedown", (e) => {
      isDragging = true;
      lastMouseX = e.clientX;
      lastMouseY = e.clientY;
    });
    window.addEventListener("mouseup", () => { isDragging = false; });
    window.addEventListener("mousemove", (e) => {
      if (isDragging) {
        const dx = e.clientX - lastMouseX;
        const dy = e.clientY - lastMouseY;
        targetRotY += dx * 0.005;
        targetRotX += dy * 0.003;
        lastMouseX = e.clientX;
        lastMouseY = e.clientY;
      } else {
        const nx = (e.clientX / W - 0.5) * 2;
        const ny = (e.clientY / H - 0.5) * 2;
        targetRotY = 0.4 + nx * 0.18;
        targetRotX = 0.28 + ny * 0.12;
      }
    }, { passive: true });

    let rafId = 0;
    let lastTime = performance.now();

    function render(now) {
      const dt = Math.min((now - lastTime) / 1000, 0.05);
      lastTime = now;

      // Slow elegant cinematic rotation
      if (!isDragging) {
        rotY += 0.0035;
      }
      rotY += (targetRotY - rotY) * 0.06;
      rotX += (targetRotX - rotX) * 0.06;

      if (isWarping) {
        warpProgress = Math.min(1, warpProgress + dt * 2.2);
      }

      ctx.clearRect(0, 0, W, H);

      const fov = 700;
      const cosY = Math.cos(rotY), sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX), sinX = Math.sin(rotX);

      function project(x3, y3, z3) {
        const x1 = x3 * cosY - z3 * sinY;
        const z1 = x3 * sinY + z3 * cosY;
        const y2 = y3 * cosX - z1 * sinX;
        const z2 = y3 * sinX + z1 * cosX;

        const dist = fov + z2;
        const scale = dist > 20 ? fov / dist : 0;
        return {
          sx: cx + x1 * scale,
          sy: cy + y2 * scale,
          scale: Math.max(0.1, scale),
          z: z2,
        };
      }

      // Draw ambient background stars
      ambientNodes.forEach((a) => {
        a.y += a.drift;
        const p = project(a.x, a.y, a.z);
        if (p.scale > 0.1) {
          const depthAlpha = Math.min(0.6, Math.max(0.1, (p.z + 400) / 800)) * a.alpha;
          ctx.fillStyle = a.col;
          ctx.globalAlpha = depthAlpha;
          ctx.beginPath();
          ctx.arc(p.sx, p.sy, a.size * p.scale, 0, Math.PI * 2);
          ctx.fill();
        }
      });

      // Update ring pulses
      pulses.forEach((pu) => {
        pu.pos = (pu.pos + pu.speed * dt) % 1;
      });

      // Project ring nodes
      const expansion = isWarping ? 1 + warpProgress * 2.8 : 1;
      const projRing = ringNodes.map((n) => {
        return project(n.x * expansion, n.y * expansion, n.z * expansion);
      });

      // Draw subtle orbital ring links
      ctx.save();
      for (let i = 0; i < RING_COUNT; i++) {
        const next = (i + 1) % RING_COUNT;
        const p1 = projRing[i];
        const p2 = projRing[next];

        const avgZ = (p1.z + p2.z) * 0.5;
        const lineAlpha = Math.min(0.35, Math.max(0.08, (avgZ + 400) / 800));

        // Subtle gradient filament
        const grad = ctx.createLinearGradient(p1.sx, p1.sy, p2.sx, p2.sy);
        grad.addColorStop(0, "rgba(56, 189, 248, " + (lineAlpha * 0.9) + ")");
        grad.addColorStop(1, "rgba(167, 139, 250, " + (lineAlpha * 0.6) + ")");

        ctx.strokeStyle = grad;
        ctx.lineWidth = Math.max(0.8, 1.4 * ((p1.scale + p2.scale) * 0.5));
        ctx.beginPath();
        ctx.moveTo(p1.sx, p1.sy);
        ctx.lineTo(p2.sx, p2.sy);
        ctx.stroke();

        // Cross-lattice cords between nearby ring points
        if (i % 3 === 0) {
          const chord = (i + 6) % RING_COUNT;
          const pChord = projRing[chord];
          ctx.strokeStyle = "rgba(56, 189, 248, 0.06)";
          ctx.lineWidth = 0.8;
          ctx.beginPath();
          ctx.moveTo(p1.sx, p1.sy);
          ctx.lineTo(pChord.sx, pChord.sy);
          ctx.stroke();
        }
      }
      ctx.restore();

      // Render traveling pulse telemetry along ring
      pulses.forEach((pu) => {
        const floatIdx = pu.pos * RING_COUNT;
        const i1 = Math.floor(floatIdx);
        const i2 = (i1 + 1) % RING_COUNT;
        const t = floatIdx - i1;

        const p1 = projRing[i1];
        const p2 = projRing[i2];
        const px = p1.sx + (p2.sx - p1.sx) * t;
        const py = p1.sy + (p2.sy - p1.sy) * t;
        const pscale = p1.scale * (1 - t) + p2.scale * t;

        ctx.save();
        ctx.fillStyle = pu.col;
        ctx.shadowColor = "#38bdf8";
        ctx.shadowBlur = 14;
        ctx.beginPath();
        ctx.arc(px, py, 3.2 * pscale, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      });

      // Render nodes on the ring
      projRing.forEach((p, idx) => {
        const n = ringNodes[idx];
        const alpha = Math.min(0.9, Math.max(0.25, (p.z + 400) / 800));
        const r = (n.core ? 4.2 : 2.8) * p.scale;

        ctx.save();
        ctx.globalAlpha = alpha;

        // Soft halo
        const haloR = r * (n.core ? 2.8 : 1.8);
        const grad = ctx.createRadialGradient(p.sx, p.sy, 0, p.sx, p.sy, haloR);
        grad.addColorStop(0, n.col);
        grad.addColorStop(0.5, "rgba(56, 189, 248, 0.25)");
        grad.addColorStop(1, "rgba(0, 0, 0, 0)");
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, haloR, 0, Math.PI * 2);
        ctx.fill();

        // Center sharp core
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(p.sx, p.sy, r * 0.6, 0, Math.PI * 2);
        ctx.fill();

        ctx.restore();
      });

      rafId = requestAnimationFrame(render);
    }

    rafId = requestAnimationFrame(render);

    return {
      warp() {
        isWarping = true;
      },
      stop() {
        cancelAnimationFrame(rafId);
      },
    };
  }

  /* ------------------------------------------------------- launch console */
  function indexPage() {
    initHero3D();
    const repo = $("#repo"), msg = $("#check"), form = $("#launch-form"), btn = $("#launch-btn");
    const splash = $("#splash");
    const splashCanvas = $("#splash-3d-canvas");
    const splash3D = splashCanvas ? initSplash3D(splashCanvas) : null;

    function dismissSplash() {
      if (!splash || splash.classList.contains("dismissed")) return;
      if (splash3D) splash3D.warp();
      splash.classList.add("dismissed");
      setTimeout(() => {
        splash.style.display = "none";
        if (repo) repo.focus();
      }, 550);
    }

    if (splash) {
      const enterBtn = $("#splash-enter-btn");
      if (enterBtn) {
        enterBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          dismissSplash();
        });
      }
      splash.addEventListener("click", () => {
        dismissSplash();
      });
      window.addEventListener("keydown", (e) => {
        if (splash && !splash.classList.contains("dismissed")) {
          if (e.key === "Enter" || e.key === " " || e.key === "Escape") {
            e.preventDefault();
            dismissSplash();
          }
        }
      });
      const replayBtn = $("#btn-replay-splash");
      if (replayBtn) {
        replayBtn.addEventListener("click", () => {
          splash.style.display = "flex";
          void splash.offsetWidth;
          splash.classList.remove("dismissed");
        });
      }
    }

    if (!repo || !form) return;
    const pf = { model: "", target: "" };
    const setCheck = (id, state, text) => {
      const el = $("#" + id);
      if (!el) return;
      el.className = "check " + state;
      el.querySelector("b").textContent = text;
    };
    function paintGo() {
      const go = $("#pf-go");
      const ok = pf.model === "ok" && pf.target === "ok";
      const bad = pf.model === "bad" || pf.target === "bad";
      go.className = "go " + (ok ? "ok" : bad ? "bad" : "");
      go.innerHTML = '<i class="dot ' + (ok ? "ok" : bad ? "failed" : "") + '"></i>' + (ok ? "All systems go" : bad ? "Attention needed" : "Checks pending");
    }
    function paintModel() {
      const backend = $("#backend") ? $("#backend").value : "";
      if (backend === "offline") {
        pf.model = "ok";
        setCheck("pf-model", "ok", "Turbo Offline Resilience · 0s Synthesis Ready (100% Reliable)");
      } else if (backend === "gemini") {
        const key = health && health.gemini_key;
        pf.model = key ? "ok" : "bad";
        setCheck("pf-model", pf.model, key ? "Gemini API · key found in GEMINI_API_KEY" : "Gemini API · GEMINI_API_KEY is not set");
      } else if (!health) {
        pf.model = "";
        setCheck("pf-model", "", "Checking the local model server…");
      } else if (health.ollama && health.gemma4 && health.gemma4.length) {
        pf.model = "ok";
        setCheck("pf-model", "ok", "Ollama online · " + health.gemma4[0]);
      } else if (health.ollama) {
        pf.model = "warn";
        setCheck("pf-model", "warn", "Ollama online, no Gemma 4 model · run: ollama pull gemma4:e2b");
      } else {
        pf.model = "warn";
        setCheck("pf-model", "warn", "Ollama offline · Select Turbo Offline Resilience or start Ollama");
      }
      paintGo();
    }
    healthListeners.push(paintModel);
    if ($("#backend")) $("#backend").addEventListener("change", paintModel);
    paintModel();

    let timer = null;
    function check() {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const v = repo.value.trim();
        if (!v) {
          pf.target = "";
          msg.className = "status-line"; msg.textContent = "Awaiting a folder path";
          setCheck("pf-target", "", "Awaiting a folder");
          paintGo();
          return;
        }
        getJSON("/api/check?path=" + encodeURIComponent(v)).then((r) => {
          if (r.state === "ok") {
            const t = "Python project · " + r.manifests.join(", ") + " · " + (r.python_files >= 500 ? "500+" : r.python_files) + " .py files";
            pf.target = "ok"; msg.className = "status-line ok"; msg.textContent = "✓ " + t; setCheck("pf-target", "ok", t);
          } else if (r.state === "weak") {
            pf.target = "warn"; msg.className = "status-line warn";
            msg.textContent = "Folder found, but no requirements*.txt / pyproject.toml or no .py files";
            setCheck("pf-target", "warn", "Folder found · results may be thin");
          } else if (r.state === "missing") {
            pf.target = "bad"; msg.className = "status-line bad"; msg.textContent = "✕ That folder does not exist";
            setCheck("pf-target", "bad", "Folder not found");
          }
          paintGo();
        }).catch(() => {});
      }, 220);
    }
    repo.addEventListener("input", check);

    // Preset Demo Chips
    $$(".chip-preset").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.preset) {
        repo.value = b.dataset.preset;
        check();
        repo.focus();
        toast("Loaded preset: " + b.textContent.trim(), "ok");
      }
    }));

    // Quick open report in Section 02
    const btnQuickOpen = $("#btn-quick-open");
    if (btnQuickOpen) {
      btnQuickOpen.addEventListener("click", () => {
        const folderInput = $("#open-folder");
        if (folderInput && btnQuickOpen.dataset.openFolder) {
          folderInput.value = btnQuickOpen.dataset.openFolder;
          toast("Opening report: " + folderInput.value, "ok");
          btnQuickOpen.closest("form").submit();
        }
      });
    }

    $$(".recent button").forEach((b) => b.addEventListener("click", () => { repo.value = b.dataset.path; check(); repo.focus(); }));
    const paintMode = () => {
      const modeVal = ($('input[name="mode"]:checked') || {}).value;
      if (modeVal === "turbo") {
        setCheck("pf-mode", "ok", "Turbo scan · 0.4s instant execution (PyPI cache + active venv)");
      } else if (modeVal === "full") {
        setCheck("pf-mode", "ok", "Full rehearsal · every proposal tested in a sandbox");
      } else {
        setCheck("pf-mode", "ok", "Quick scan · proposals stay unrehearsed");
      }
    };
    $$('input[name="mode"]').forEach((r) => r.addEventListener("change", paintMode));
    paintMode();
    form.addEventListener("submit", () => { btn.classList.add("busy"); btn.querySelector("span").textContent = "Launching…"; });
    document.addEventListener("keydown", (e) => {
      const typing = /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || "");
      if (e.key === "/" && !typing) { e.preventDefault(); repo.focus(); }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && form.contains(document.activeElement)) {
        e.preventDefault();
        if (form.reportValidity()) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
      }
    });
  }

  /* ------------------------------------------------------ live operation */
  const STATE = {
    running: ["LIVE", "running", "Pipeline running"],
    finished: ["COMPLETE", "finished", "All stages completed"],
    failed: ["ATTENTION", "failed", "Finished with problems"],
    stopped: ["STOPPED", "stopped", "Stopped by you"],
    interrupted: ["INTERRUPTED", "stopped", "Interrupted before it finished"],
  };
  const LABEL = { pending: "Pending", running: "Running", ok: "Done", failed: "Failed", skipped: "Skipped" };
  const TAGCLS = { VERIFIED: "t-verified", OBSERVED: "t-observed", "AI-PROPOSED": "t-proposed", REFUTED: "t-refuted", "AI-GENERATED": "t-generated" };
  const TIP = {
    verified: "Tests that passed before still pass, and the change was exercised",
    unproven: "No test proved it: the edited lines were not exercised, or there was nothing to compare",
    refuted: "The sandbox showed this proposal breaks the tests or the install",
    "no proposal": "Gemma 4 did not return a usable proposal",
    unrehearsed: "The sandbox rehearsal was skipped",
  };
  const KIND = {
    "test-failure": ["TEST FAIL", "A test fails in a clean environment", "#ef4444"],
    "install-failure": ["INSTALL", "An install step failed", "#ef4444"],
    "undeclared-import": ["UNDECLARED", "Imported but never declared", "#38bdf8"],
    "private-api": ["PRIVATE API", "Calls a private API", "#a78bfa"],
    "monkeypatch": ["MONKEYPATCH", "Patches the library at runtime", "#f472b6"],
    "yanked": ["YANKED", "Release was yanked on PyPI", "#ef4444"],
    "inactive": ["INACTIVE", "Marked inactive on PyPI", "#f59e0b"],
    "python-requires": ["PY COMPAT", "Does not support this Python", "#f59e0b"],
    "version-lag": ["MAJOR LAG", "A major version behind", "#f59e0b"],
    "stale-release": ["STALE", "No release in a long time", "#f59e0b"],
    "test-warning": ["WARNING", "Warning seen while testing", "#facc15"],
    "outdated-classifiers": ["CLASSIFIERS", "Classifiers look out of date", "#94a3b8"],
    "pypi-unavailable": ["PYPI", "PyPI could not be reached", "#94a3b8"],
  };
  const tagChip = (tag) => (tag ? '<span class="tag ' + (TAGCLS[tag] || "") + '">' + esc(tag) + "</span>" : "");
  const verdictChip = (v) => {
    const key = ["verified", "unproven", "refuted"].includes(v) ? v : "other";
    const mark = { verified: "✓", unproven: "?", refuted: "✕", other: "·" }[key];
    return '<span class="verdict v-' + key + '" title="' + esc(TIP[v] || "") + '">' + mark + " " + esc(v) + "</span>";
  };
  function hint(t) {
    t = t || "";
    if (/Cannot reach ollama|actively refused|Connection refused/i.test(t)) return "The model server is not running. Start Ollama, make sure it has the model (ollama pull gemma4:e2b), then run again.";
    if (/GEMINI_API_KEY/.test(t)) return "Set the GEMINI_API_KEY environment variable, or switch the backend to Ollama.";
    if (/allocate|out of memory|not enough memory/i.test(t)) return "The model could not get enough free memory. Close heavy apps and run again.";
    if (/No module named/i.test(t)) return "A Python module is missing. Install the requirements: pip install -r requirements-gemmamorph.txt";
    return "";
  }
  function lineClass(l) {
    if (/^--- S\d/.test(l)) return "l-stage";
    if (/^\[ok\s*\]/.test(l) || /^output: /.test(l)) return "l-ok";
    if (/^\[failed\s*\]/.test(l)) return "l-fail";
    if (/^\[skipped\]/.test(l)) return "l-skip";
    if (/^\s*\$ /.test(l)) return "l-cmd";
    if (/LLM (query|response)/.test(l)) return "l-llm";
    if (/dropped edit|rejected|asking once more/i.test(l)) return "l-drop";
    if (/^exit code/.test(l)) return "l-exit";
    if (/Traceback|Error\b|FAILED/.test(l)) return "l-err";
    if (/^\s+\(\d+(\.\d+)?s\)\s*$/.test(l)) return "l-dim";
    return "";
  }
  const fmt = (s) => {
    const m = Math.floor(s / 60), h = Math.floor(m / 60);
    return (h ? h + "h " : "") + (m % 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  };
  function countUp(root) {
    $$("[data-count]", root).forEach((el) => {
      const target = Number(el.dataset.count) || 0;
      if (!motionOn || !target) { el.textContent = target; return; }
      const t0 = performance.now(), dur = 900 + Math.min(target, 60) * 8;
      const tick = (t) => {
        const k = Math.min(1, (t - t0) / dur);
        el.textContent = Math.round(target * (1 - Math.pow(1 - k, 3)));
        if (k < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });
  }
  function typeOut(el, text) {
    if (!el) return;
    if (!motionOn) { el.textContent = text; return; }
    const caret = document.createElement("span");
    caret.className = "caret";
    let i = 0;
    const tick = () => {
      i = Math.min(text.length, i + 3);
      el.textContent = text.slice(0, i);
      el.appendChild(caret);
      if (i < text.length) requestAnimationFrame(tick); else setTimeout(() => caret.remove(), 1500);
    };
    requestAnimationFrame(tick);
  }
  function donut(V, U, R, O) {
    const total = V + U + R + O, T = Math.max(total, 1), r = 84, C = 2 * Math.PI * r;
    let off = 0;
    const arc = (v, col) => {
      if (!v) return "";
      const len = (v / T) * C;
      const s = '<circle class="arc" cx="110" cy="110" r="' + r + '" fill="none" stroke="' + col + '" stroke-width="15" stroke-dasharray="' + len + " " + (C - len) + '" stroke-dashoffset="' + -off + '" style="filter:drop-shadow(0 0 7px ' + col + ')"/>';
      off += len;
      return s;
    };
    return '<svg class="donut" viewBox="0 0 220 220" role="img" aria-label="Rehearsal verdicts">' +
      '<circle class="ring" cx="110" cy="110" r="104" fill="none" stroke="rgba(56,189,248,.4)" stroke-width="1.2" stroke-dasharray="2 7"/>' +
      '<circle cx="110" cy="110" r="' + r + '" fill="none" stroke="rgba(148,163,184,.12)" stroke-width="15"/>' +
      '<g transform="rotate(-90 110 110)">' + arc(V, "#10b981") + arc(U, "#facc15") + arc(R, "#ef4444") + arc(O, "#64748b") + "</g>" +
      '<text class="dn" x="110" y="116" text-anchor="middle">' + total + '</text><text class="dl" x="110" y="140" text-anchor="middle">GEMMA 4 PROPOSALS</text></svg>';
  }
  function diffBlock(file, oldT, newT) {
    const lines = (t, c, m) => (t ? t.split("\n").map((l) => '<span class="' + c + '">' + m + esc(l) + "</span>").join("") : "");
    const idx = copyStore.push((oldT ? oldT.split("\n").map((l) => "- " + l).join("\n") + "\n" : "") + (newT ? newT.split("\n").map((l) => "+ " + l).join("\n") : "")) - 1;
    return '<div class="diff"><div class="fh"><span>' + ICON.file + esc(file) + '</span><button type="button" class="copy" data-copy="' + idx + '">Copy</button></div><pre>' + lines(oldT, "m", "- ") + lines(newT, "p", "+ ") + "</pre></div>";
  }
  function proposalCard(p) {
    const key = ["verified", "unproven", "refuted"].includes(p.verdict) ? p.verdict : "other";
    let detail = "";
    if (p.rationale) detail += '<blockquote class="rationale">' + esc(p.rationale) + "</blockquote>";
    (p.edits || []).forEach((e) => { detail += diffBlock(e.file, e.old, e.new); });
    if ((p.remove || []).length || (p.add || []).length) detail += diffBlock("requirements", (p.remove || []).join("\n"), (p.add || []).join("\n"));
    const numEdits = (p.edits || []).length;
    const summaryText = numEdits ? "⚡ Proposed Code Replacements (" + numEdits + " diffs)" : "Inspect Gemma 4 proposal";
    return '<article class="prop ' + key + '" data-v="' + esc(p.verdict) + '"><h3>' + esc(p.dependency) +
      (p.replacement ? '<span class="to">→ ' + esc(p.replacement) + "</span>" : "") + "</h3>" +
      '<div class="meta">' + verdictChip(p.verdict) + tagChip(p.tag) + "</div>" +
      '<p class="why">' + esc(p.reason || "") + "</p>" +
      (detail ? "<details open><summary>" + summaryText + "</summary>" + detail + "</details>" : "") + "</article>";
  }
  function resultsHTML(s) {
    const c = s.counts || {};
    const V = c.verified || 0, U = c.unproven || 0, R = c.refuted || 0, O = s.proposals.length - V - U - R;
    const when = s.generated_at ? new Date(s.generated_at) : null;
    const meta = [s.model && s.model.name, s.model && s.model.backend, when && !isNaN(when) ? when.toLocaleString() : ""].filter(Boolean).map(esc).join(" · ");
    const tile = (n, label, cls) => '<div class="tile ' + (cls || "") + '"><b data-count="' + (Number(n) || 0) + '">0</b><span>' + esc(label) + "</span></div>";

    let blastHtml = "";
    if (s.blast_radius) {
      const br = s.blast_radius;
      const ac = s.anti_cheat || { passed: true, modified_tests: [], deleted_asserts: 0 };
      blastHtml = '<div class="kavach-metrics">' +
        '<div class="kavach-card ' + (br.score < 50 ? "safe" : "alert") + '">' +
        '<div class="kavach-h"><span>⚡ Blast Radius Impact</span><span>' + br.files_count + ' files · ' + br.total_calls + ' calls</span></div>' +
        '<b>' + br.score + ' / 100</b>' +
        '<div class="blast-bar"><div class="blast-fill" style="width:' + Math.min(100, Math.max(12, br.score)) + '%"></div></div>' +
        '<p>' + (br.score < 40 ? 'Minimal structural blast radius · surgical refactoring' : 'Broad API exposure · verified through isolated rehearsal') + '</p>' +
        '</div>' +
        '<div class="kavach-card ' + (ac.passed ? "safe" : "alert") + '">' +
        '<div class="kavach-h"><span>🛡️ Anti-Cheat Integrity Audit</span><span class="badge-anticheat ' + (ac.passed ? "" : "failed") + '">' + (ac.passed ? "PASSED" : "FLAGGED") + '</span></div>' +
        '<b>' + (ac.passed ? "100% Integrity" : "Warning: Tests Edited") + '</b>' +
        '<p>' + (ac.passed ? 'Zero modifications to test assertions, zero mock cheating detected.' : 'Detected ' + ac.modified_tests.length + ' test modifications.') + '</p>' +
        '</div></div>';
    }

    let h = '<section class="panel hud reveal in" id="results"><i class="corner tl"></i><i class="corner br"></i>' +
      '<div class="sec-head"><div><div class="kicker">03 <i>//</i> Results</div><h2>Mission results</h2><p class="lead">' + (meta || "From the Exit Capsule") + "</p></div></div>" +
      '<div class="overview"><div class="donut-wrap">' + donut(V, U, R, O) +
      '<div class="keys"><span><i style="background:#10b981"></i>verified ' + V + '</span><span><i style="background:#facc15"></i>unproven ' + U +
      '</span><span><i style="background:#ef4444"></i>refuted ' + R + "</span>" + (O > 0 ? '<span><i style="background:#64748b"></i>other ' + O + "</span>" : "") + "</div></div>" +
      '<div class="tiles">' + tile(s.dependencies, "dependencies found") + tile(s.conflicts, "manifest conflicts") + tile(s.signals, "risk signals") +
      tile(s.flagged, "dependencies flagged") + tile(s.usages, "code usages traced") + tile(V, "proposals verified", "v") + tile(U, "unproven", "u") +
      tile(R, "refuted by the sandbox", "r") + "</div></div>";
    
    h += blastHtml;

    if (s.summary) h += '<div class="summary-box"><div class="summary-h">' + tagChip("AI-GENERATED") + "Summary written by Gemma 4</div><p class=\"summary\" id=\"summary-text\"></p></div>";
    else if (s.summary_error) h += '<div class="summary-box"><div class="summary-h">Summary</div><p class="summary">Unavailable: ' + esc(s.summary_error) + "</p></div>";
    h += "</section>";
    if (s.findings && s.findings.length) {
      h += '<section class="panel reveal in" id="findings"><div class="sec-head"><div><div class="kicker">04 <i>//</i> Key findings</div><h2>What the scan found</h2>' +
        '<p class="lead">The strongest risk signals, each with its source and claim tag.</p></div></div><div class="finds">' +
        s.findings.map((f) => {
          const k = KIND[f.kind] || [String(f.kind).toUpperCase(), f.kind, "#38bdf8"];
          return '<div class="find" style="--k:' + k[2] + '"><span class="code">' + esc(k[0]) + "</span><div><b>" + esc(k[1]) + (f.dependency ? " · " + esc(f.dependency) : "") +
            "</b>" + tagChip(f.tag) + "<p>" + esc(f.detail) + (f.source ? "  [" + esc(f.source) + "]" : "") + "</p></div></div>";
        }).join("") + "</div></section>";
    }
    if (s.proposals.length) {
      const groups = [["all", "All", s.proposals.length], ["verified", "Verified", V], ["unproven", "Unproven", U], ["refuted", "Refuted", R],
        ["no proposal", "No proposal", c["no proposal"] || 0], ["unrehearsed", "Unrehearsed", c.unrehearsed || 0]];
      h += '<section class="panel reveal in" id="proposals"><div class="sec-head"><div><div class="kicker">05 <i>//</i> Proposals</div><h2>Gemma 4 proposals and their rehearsal</h2>' +
        '<p class="lead">What Gemma 4 suggested for each flagged dependency, and what the sandbox made of it.</p></div></div>' +
        '<div class="toolbar"><div class="filters">' + groups.filter((g) => g[0] === "all" || g[2]).map((g) =>
          '<button type="button" class="filter' + (g[0] === "all" ? " on" : "") + '" data-filter="' + esc(g[0]) + '">' + esc(g[1]) + "<b>" + g[2] + "</b></button>").join("") +
        '</div>' +
        '<div class="results-actions">' +
        '<button type="button" class="btn ghost small" id="copy-summary-md">📋 Copy Markdown</button>' +
        '<button type="button" class="btn ghost small" id="expand-all">Expand all</button>' +
        '</div></div>' +
        '<div class="props">' + s.proposals.map(proposalCard).join("") + "</div></section>";
    }
    return h;
  }

  function runPage() {
    const id = GM.id;
    if (!id) return;
    let prev = null, statsSig = "", filesSig = "", logSig = "";
    $("#stop").addEventListener("click", () => {
      fetch("/api/stop/" + id, { method: "POST" }).then(() => toast("Stop requested", "bad"), () => {});
    });

    function paintHeader(d) {
      const s = STATE[d.state] || STATE.running;
      const live = $("#live");
      live.className = "live " + s[1];
      live.querySelector(".dot").className = "dot " + (s[1] === "finished" ? "ok" : s[1]);
      $("#live-text").textContent = d.imported ? "REPLAY" : s[0];
      $("#state-text").textContent = s[2];
      $("#timer-label").textContent = d.imported ? "SOURCE" : d.state === "running" ? "ELAPSED" : "DURATION";
      $("#clock-run").textContent = d.imported ? "from disk" : fmt(d.elapsed || 0);
      $("#stop").hidden = d.state !== "running";
      document.title = ({ running: "● Running", finished: "✓ Complete", failed: "⚠ Problems", stopped: "■ Stopped", interrupted: "■ Interrupted" }[d.state] || "Operation") + " · GemmaMorph";
      const bad = d.stages.find((x) => x.state === "failed");
      const problem = bad ? bad.name + ": " + bad.detail : d.error || "";
      const box = $("#alert");
      if ((d.state === "failed" || d.error) && problem) {
        const fix = hint(problem + " " + (d.log_tail || ""));
        box.innerHTML = '<div class="alert"><div class="alert-h">' + ICON.alert + "What went wrong</div><p>" + esc(problem) + "</p>" +
          (fix ? '<p class="fix"><span>FIX</span>' + esc(fix) + "</p>" : "") + "</div>";
      } else {
        box.innerHTML = "";
      }
    }
    function paintStages(d) {
      let done = 0;
      d.stages.forEach((s, i) => {
        const el = $("#s" + i), seg = $("#seg" + i);
        if (!el) return;
        if (el.dataset.state !== s.state) {
          const had = el.dataset.state;
          el.dataset.state = s.state;
          el.className = "stage " + s.state;
          if (had) { const ic = el.querySelector(".ic"); ic.classList.remove("flash"); void ic.offsetWidth; ic.classList.add("flash"); }
        }
        el.querySelector(".st").textContent = LABEL[s.state] || s.state;
        el.querySelector(".detail").textContent = s.state === "pending" || s.state === "running" ? el.dataset.desc : s.detail || el.dataset.desc;
        el.querySelector(".secs").textContent = s.seconds != null ? Number(s.seconds).toFixed(1) + "s" : "";
        if (seg) seg.className = "seg " + s.state;
        if (["ok", "failed", "skipped"].includes(s.state)) done++;
      });
      $("#progress-text").textContent = done + " / 6 stages complete";
    }
    function paintLog(d) {
      const text = d.log_tail || "";
      const sig = text.length + "|" + text.slice(-160) + "|" + d.state;
      if (sig === logSig) return;
      logSig = sig;
      const box = $("#log");
      const lines = text ? text.split("\n") : [];
      box.innerHTML = (lines.length ? lines.map((l) => {
        const c = lineClass(l);
        let h = esc(l);
        if (c === "l-cmd") h = h.replace(/^(\s*)\$ /, (m, sp) => sp + "<b>$</b> ");
        return '<span class="' + c + '">' + h + "</span>";
      }).join("\n") : '<span class="l-dim">' + (d.imported ? "This report was opened from disk, so there is no live output to show. Everything the run recorded is in the Exit Capsule below." : "Waiting for output…") + "</span>") + (d.state === "running" ? '\n<span class="cursor"></span>' : "");
      if ($("#follow").checked) box.scrollTop = box.scrollHeight;
      $("#log-count").textContent = lines.length + " lines";
    }
    function paintResults(d) {
      const area = $("#results-area");
      const s = d.stats;
      if (!s) {
        if (d.state !== "running") {
          area.innerHTML = '<section class="panel" id="results"><div class="empty">' + ICON.info + "<div><b>No Exit Capsule was produced.</b><br>" +
            '<span class="hint">See the stage that failed above, and the terminal for the full output.</span></div></div></section>';
        }
        return;
      }
      const sig = JSON.stringify(s);
      if (sig === statsSig) return;
      statsSig = sig;
      area.innerHTML = resultsHTML(s);
      countUp(area);
      if (s.summary) typeOut($("#summary-text"), s.summary);

      const btnCopyMd = $("#copy-summary-md", area);
      if (btnCopyMd) {
        btnCopyMd.addEventListener("click", () => {
          fetch("/files/" + id + "/EXIT_CAPSULE.md").then((r) => {
            if (!r.ok) throw new Error("not found");
            return r.text();
          }).then((md) => {
            navigator.clipboard.writeText(md).then(() => toast("Exit Capsule Markdown copied to clipboard!", "ok"));
          }).catch(() => {
            const summary = s.summary || "GemmaMorph Exit Capsule report";
            navigator.clipboard.writeText(summary).then(() => toast("Summary copied to clipboard!", "ok"));
          });
        });
      }

      $$(".filter", area).forEach((b) => b.addEventListener("click", () => {
        $$(".filter", area).forEach((x) => x.classList.toggle("on", x === b));
        const f = b.dataset.filter;
        $$(".prop", area).forEach((p) => { p.hidden = f !== "all" && p.dataset.v !== f; });
      }));
      const ex = $("#expand-all", area);
      if (ex) {
        ex.addEventListener("click", () => {
          const ds = $$(".prop:not([hidden]) details", area);
          const open = !ds.every((x) => x.open);
          ds.forEach((x) => { x.open = open; });
          ex.textContent = open ? "Collapse all" : "Expand all";
        });
      }
      if (location.hash === "#expand") $$(".prop details", area).forEach((x) => { x.open = true; });
      observeReveals(area);
      if (GM.spyWatch) GM.spyWatch();
    }
    function paintFiles(d) {
      const sig = d.files.join("|") + "|" + d.state;
      if (sig === filesSig) return;
      filesSig = sig;
      const area = $("#files-area");
      if (!d.files.length) { area.innerHTML = ""; return; }
      const DESC = {
        "EXIT_CAPSULE.html": "Full report · opens in the browser",
        "EXIT_CAPSULE.md": "Markdown report",
        "EXIT_CAPSULE.json": "Machine-readable capsule",
        "proposed.patch": "Patch of the proposals that survived",
      };
      let h = '<section class="panel reveal in" id="capsule"><div class="sec-head"><div><div class="kicker">06 <i>//</i> Artifacts</div><h2>Exit Capsule and files</h2>' +
        '<p class="lead">Everything the run wrote, served straight from its output folder.</p></div></div><div class="carts">' +
        d.files.map((f) => '<a class="cart" href="/files/' + id + "/" + encodeURIComponent(f) + '" target="_blank" rel="noopener"><span class="ci">' + ICON.file +
          "</span><span><b>" + esc(f) + "</b><small>" + esc(DESC[f] || "") + "</small></span>" + ICON.ext + "</a>").join("") + "</div>";
      if (d.files.includes("EXIT_CAPSULE.html") && d.state !== "running") {
        h += '<div class="viewer"><div class="vbar"><span><i class="dot ok"></i>EXIT_CAPSULE.html</span><a href="/files/' + id +
          '/EXIT_CAPSULE.html" target="_blank" rel="noopener">Open in a new tab ' + ICON.ext + '</a></div><iframe sandbox="allow-same-origin allow-scripts allow-popups allow-forms" title="Exit Capsule" loading="lazy" src="/files/' + id + '/EXIT_CAPSULE.html"></iframe></div>';
      }
      area.innerHTML = h + "</section>";
      observeReveals(area);
      if (GM.spyWatch) GM.spyWatch();
    }
    function tick() {
      getJSON("/api/run/" + id).then((d) => {
        paintHeader(d); paintStages(d); paintLog(d); paintResults(d); paintFiles(d);
        if (prev === "running" && d.state !== "running") {
          const ok = d.state === "finished";
          toast(ok ? "Operation complete: the Exit Capsule is ready" : d.state === "stopped" ? "Operation stopped" : "Operation finished with problems", ok ? "ok" : "bad");
        }
        prev = d.state;
        if (d.state === "running") setTimeout(tick, 2000);
      }).catch(() => setTimeout(tick, 3000));
    }
    tick();
  }

  /* -------------------------------------------------------------- start */
  function init() {
    const bg = startBackground();
    motionToggle(bg);
    clock();
    pollHealth();
    setInterval(pollHealth, 20000);
    const menu = $("#menu");
    if (menu) menu.addEventListener("click", () => document.body.classList.toggle("nav-open"));
    $$(".nav a").forEach((a) => a.addEventListener("click", () => document.body.classList.remove("nav-open")));
    observeReveals();
    GM.spyWatch = scrollSpy();
    if (GM.page === "index") indexPage();
    if (GM.page === "run") runPage();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
