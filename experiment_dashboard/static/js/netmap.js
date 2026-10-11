// Shared road-network canvas: Control bottleneck preview + Analyze maps.
(function (global) {
  function setup(canvas, geo, height) {
    const b = geo.bounds || { minX: 0, minY: 0, maxX: 1, maxY: 1 };
    const pad = 22;
    const W = canvas.clientWidth || 800;
    const H = height || 540;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const bw = (b.maxX - b.minX) || 1;
    const bh = (b.maxY - b.minY) || 1;
    const scale = Math.min((W - 2 * pad) / bw, (H - 2 * pad) / bh);
    const offX = pad + ((W - 2 * pad) - bw * scale) / 2;
    const offY = pad + ((H - 2 * pad) - bh * scale) / 2;
    const tx = x => offX + (x - b.minX) * scale;
    const ty = y => offY + (b.maxY - y) * scale;  // flip y (SUMO y is up)

    const stroke = (points, color, width) => {
      if (!points || points.length < 2) return;
      ctx.beginPath();
      ctx.moveTo(tx(points[0][0]), ty(points[0][1]));
      for (let i = 1; i < points.length; i++) ctx.lineTo(tx(points[i][0]), ty(points[i][1]));
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.stroke();
    };

    const dot = (pt, color, r) => {
      if (!pt) return;
      ctx.beginPath();
      ctx.arc(tx(pt[0]), ty(pt[1]), r || 5, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#fff';
      ctx.stroke();
    };

    return { stroke, dot };
  }

  function edgePoints(geo) {
    const m = new Map();
    (geo.edges || []).forEach(e => m.set(e.id, e.points));
    return m;
  }

  function drawJam(canvas, geo, usage, jamEdges, height) {
    const p = setup(canvas, geo, height);
    const pts = edgePoints(geo);
    let max = 0;
    Object.values(usage || {}).forEach(v => { if (v > max) max = v; });
    (geo.edges || []).forEach(e => {
      const t = max > 0 ? Math.sqrt((usage[e.id] || 0) / max) : 0;
      p.stroke(e.points, `rgba(120,132,150,${0.25 + t * 0.5})`, 1 + t * 4);
    });
    (jamEdges || []).forEach(id => p.stroke(pts.get(id), '#e03131', 5));
  }

  const _geoCache = {};
  function geometry(network) {
    if (!_geoCache[network]) {
      _geoCache[network] = fetch('/api/network/' + encodeURIComponent(network) + '/geometry')
        .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
        .catch(e => { delete _geoCache[network]; throw e; });
    }
    return _geoCache[network];
  }

  global.NetMap = { setup, edgePoints, drawJam, geometry };
})(window);
