(function (global) {
  "use strict";

  var U = global.GameUtils;
  var particles = [];
  var floatTexts = [];
  var shakeMag = 0;
  var shakeT = 0;
  var shakeT0 = 0.15;
  var shakeCur = 0;

  function clear() {
    particles.length = 0;
    floatTexts.length = 0;
    shakeMag = 0;
    shakeT = 0;
    shakeT0 = 0.15;
    shakeCur = 0;
  }

  function addShake(mag, duration) {
    var d = duration || 0.14;
    shakeMag = Math.max(shakeMag, mag);
    shakeT0 = Math.max(shakeT0, d);
    shakeT = Math.max(shakeT, d);
  }

  function getShake() {
    if (shakeCur <= 0) return { x: 0, y: 0 };
    var m = shakeCur;
    return {
      x: (Math.random() - 0.5) * m * 2.2,
      y: (Math.random() - 0.5) * m * 2.2,
    };
  }

  function spawnParticle(o) {
    particles.push(o);
  }

  function burst(x, y, count, cr, cg, cb, speedMin, speedMax) {
    for (var i = 0; i < count; i++) {
      var a = U.rand(0, Math.PI * 2);
      var sp = U.rand(speedMin, speedMax);
      particles.push({
        x: x,
        y: y,
        vx: Math.cos(a) * sp,
        vy: Math.sin(a) * sp,
        life: U.rand(0.15, 0.55),
        max: U.rand(0.15, 0.55),
        r: cr,
        g: cg,
        b: cb,
        size: U.rand(2, 7),
        drag: 0.96,
        grav: U.rand(120, 320),
      });
    }
  }

  function ring(x, y, cr, cg, cb) {
    particles.push({
      kind: "ring",
      x: x,
      y: y,
      r: 8,
      vr: 420,
      life: 0.35,
      cr: cr,
      cg: cg,
      cb: cb,
    });
  }

  function muzzleFlash(x, y, dir, cr, cg, cb) {
    burst(x, y, 10 + U.randInt(0, 6), cr, cg, cb, 60, 220);
    ring(x, y, cr, cg, cb);
    for (var k = 0; k < 4; k++) {
      particles.push({
        x: x,
        y: y,
        vx: dir * U.rand(180, 380),
        vy: U.rand(-40, 40),
        life: 0.12,
        max: 0.12,
        r: 255,
        g: 255,
        b: 255,
        size: U.rand(3, 8),
        drag: 0.88,
        grav: 0,
      });
    }
  }

  function landDust(x, y) {
    burst(x, y, 14, 180, 170, 150, 20, 120);
    addShake(2.5, 0.08);
  }

  function hitSparks(x, y) {
    burst(x, y, 18, 255, 220, 80, 80, 320);
    addShake(3.5, 0.1);
  }

  function beamSparks(x, y, w, h) {
    for (var i = 0; i < 8; i++) {
      burst(
        x + U.rand(0, w),
        y + U.rand(0, h),
        4,
        120,
        220,
        255,
        40,
        160
      );
    }
  }

  function update(dt) {
    if (shakeT > 0) {
      shakeT -= dt;
      shakeCur = shakeMag * U.clamp(shakeT / Math.max(shakeT0, 0.05), 0, 1);
      if (shakeT <= 0) {
        shakeT = 0;
        shakeMag = 0;
        shakeCur = 0;
        shakeT0 = 0.15;
      }
    } else {
      shakeCur = 0;
    }

    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.life -= dt;
      if (p.kind === "ring") {
        p.r += p.vr * dt;
        if (p.life <= 0) particles.splice(i, 1);
        continue;
      }
      p.vx *= p.drag != null ? Math.pow(p.drag, dt * 60) : 1;
      p.vy += (p.grav || 0) * dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      if (p.life <= 0) particles.splice(i, 1);
    }
    for (var j = floatTexts.length - 1; j >= 0; j--) {
      var ft = floatTexts[j];
      ft.life -= dt;
      ft.y += ft.vy * dt;
      if (ft.life <= 0) floatTexts.splice(j, 1);
    }
  }

  function draw(ctx) {
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      if (p.kind === "ring") {
        var al = Math.max(0, p.life / 0.35);
        ctx.strokeStyle =
          "rgba(" + p.cr + "," + p.cg + "," + p.cb + "," + (al * 0.85) + ")";
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.stroke();
        continue;
      }
      var t = p.life / (p.max || 0.4);
      ctx.globalAlpha = U.clamp(t * 1.4, 0, 1);
      ctx.fillStyle = "rgb(" + p.r + "," + p.g + "," + p.b + ")";
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * (0.5 + t * 0.5), 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.font = "bold 16px Microsoft YaHei";
    ctx.textAlign = "center";
    for (var j = 0; j < floatTexts.length; j++) {
      var ft = floatTexts[j];
      ctx.globalAlpha = U.clamp(ft.life / 0.5, 0, 1);
      ctx.fillStyle = ft.color;
      ctx.fillText(ft.text, ft.x, ft.y);
      ctx.globalAlpha = 1;
    }
    ctx.textAlign = "left";
  }

  function floatText(x, y, text, color) {
    floatTexts.push({
      x: x,
      y: y,
      text: text,
      color: color || "#fff59d",
      life: 0.55,
      vy: -35,
    });
  }

  global.FX = {
    clear: clear,
    update: update,
    draw: draw,
    addShake: addShake,
    getShake: getShake,
    spawnParticle: spawnParticle,
    burst: burst,
    ring: ring,
    muzzleFlash: muzzleFlash,
    landDust: landDust,
    hitSparks: hitSparks,
    beamSparks: beamSparks,
    floatText: floatText,
  };
})(typeof window !== "undefined" ? window : globalThis);
