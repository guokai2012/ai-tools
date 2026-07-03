(function (global) {
  "use strict";

  var U = global.GameUtils;
  var LEVELS = global.LevelData.LEVELS;

  var W = 960;
  var H = 540;
  var GROUND_Y = 420;

  function createCanvas(parent) {
    var c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    parent.appendChild(c);
    return c;
  }

  function skyGradient(ctx, bgKey) {
    var g = ctx.createLinearGradient(0, 0, 0, H);
    switch (bgKey) {
      case "earth":
        g.addColorStop(0, "#5eb8ff");
        g.addColorStop(0.35, "#9ee0ff");
        g.addColorStop(0.58, "#7ec87a");
        g.addColorStop(0.78, "#3a8c3a");
        g.addColorStop(1, "#143218");
        break;
      case "moon":
        g.addColorStop(0, "#080818");
        g.addColorStop(0.45, "#1a1a32");
        g.addColorStop(1, "#2a2a44");
        break;
      case "mars":
        g.addColorStop(0, "#e07040");
        g.addColorStop(0.45, "#a04028");
        g.addColorStop(1, "#3a1208");
        break;
      case "jupiter":
        g.addColorStop(0, "#e8c090");
        g.addColorStop(0.3, "#c08050");
        g.addColorStop(0.65, "#704020");
        g.addColorStop(1, "#281808");
        break;
      case "saturn":
        g.addColorStop(0, "#120820");
        g.addColorStop(0.5, "#402858");
        g.addColorStop(1, "#a89878");
        break;
      case "uranus":
        g.addColorStop(0, "#60e8ff");
        g.addColorStop(0.5, "#2080b0");
        g.addColorStop(1, "#082040");
        break;
      case "neptune":
        g.addColorStop(0, "#3048c0");
        g.addColorStop(0.5, "#182868");
        g.addColorStop(1, "#040818");
        break;
      case "pluto":
        g.addColorStop(0, "#12121c");
        g.addColorStop(1, "#08080e");
        break;
      case "m78orbit":
        g.addColorStop(0, "#000018");
        g.addColorStop(0.45, "#183060");
        g.addColorStop(1, "#2858a0");
        break;
      case "m78":
      default:
        g.addColorStop(0, "#fff8a0");
        g.addColorStop(0.2, "#ffe066");
        g.addColorStop(0.5, "#b8e8ff");
        g.addColorStop(1, "#2080d0");
        break;
    }
    return g;
  }

  function drawParallaxStars(ctx, bgKey, t) {
    var scroll = t * 35;
    ctx.save();
    for (var i = 0; i < 100; i++) {
      var layer = (i % 3) + 1;
      var sp = scroll * (0.08 + layer * 0.04);
      var sx = ((i * 131 + sp) % (W + 20)) - 10;
      var sy = (i * 47) % (GROUND_Y - 20);
      var sz = (i % 4) * 0.5 + 0.5;
      ctx.globalAlpha = 0.25 + (i % 7) * 0.08;
      ctx.fillStyle =
        bgKey === "m78" || bgKey === "m78orbit"
          ? "#fffde7"
          : "#e8f4ff";
      ctx.fillRect(sx, sy, sz, sz);
    }
    ctx.restore();
  }

  function drawFarSilhouette(ctx, bgKey, t) {
    var s = t * 18;
    var base = GROUND_Y - 40;
    ctx.save();
    ctx.globalAlpha = 0.55;
    if (bgKey === "earth") {
      ctx.fillStyle = "#1a3a1a";
      for (var x = -80; x < W + 80; x += 60) {
        var h = 50 + Math.sin(x * 0.02 + s * 0.01) * 25;
        ctx.beginPath();
        ctx.moveTo(x, base);
        ctx.lineTo(x + 30, base - h);
        ctx.lineTo(x + 60, base);
        ctx.closePath();
        ctx.fill();
      }
    } else if (bgKey === "moon" || bgKey === "pluto") {
      ctx.fillStyle = "#252530";
      for (var m = -100; m < W + 100; m += 90) {
        var cr = 35 + Math.abs((m % 40) - 20);
        ctx.beginPath();
        ctx.moveTo(m, base);
        ctx.arc(m + 45, base, cr, Math.PI, 0);
        ctx.closePath();
        ctx.fill();
      }
    } else if (bgKey === "mars") {
      ctx.fillStyle = "#4a1810";
      for (var r = -120; r < W + 120; r += 100) {
        ctx.beginPath();
        ctx.moveTo(r, base);
        ctx.lineTo(r + 50, base - 70);
        ctx.lineTo(r + 100, base);
        ctx.closePath();
        ctx.fill();
      }
    } else if (bgKey === "saturn") {
      ctx.fillStyle = "#1a1020";
      ctx.beginPath();
      ctx.ellipse(W * 0.5, base - 20, W * 0.55, 45, 0, 0, Math.PI * 2);
      ctx.fill();
    } else if (bgKey === "m78" || bgKey === "m78orbit") {
      ctx.globalAlpha = 0.4;
      ctx.fillStyle = "rgba(255,220,120,0.28)";
      ctx.fillRect(0, 80, W, 100);
      ctx.fillStyle = "rgba(255,255,255,0.14)";
      for (var c = 0; c < 5; c++) {
        ctx.fillRect(c * 220 + ((s * 0.5) % 220), 100, 80, 8);
      }
    } else {
      ctx.fillStyle = "rgba(0,20,40,0.45)";
      for (var w = -60; w < W + 60; w += 70) {
        ctx.beginPath();
        ctx.moveTo(w, base);
        ctx.lineTo(w + 35, base - 45);
        ctx.lineTo(w + 70, base);
        ctx.closePath();
        ctx.fill();
      }
    }
    ctx.restore();
  }

  function drawMidClouds(ctx, bgKey, t) {
    var sc = t * 28;
    ctx.save();
    ctx.globalAlpha = 0.35;
    if (bgKey === "earth") {
      ctx.fillStyle = "rgba(255,255,255,0.5)";
      for (var i = 0; i < 6; i++) {
        var cx = ((i * 200 + sc * 0.15) % (W + 180)) - 90;
        var cy = 90 + (i % 3) * 40;
        ctx.beginPath();
        ctx.arc(cx, cy, 28, 0, Math.PI * 2);
        ctx.arc(cx + 35, cy - 5, 32, 0, Math.PI * 2);
        ctx.arc(cx + 70, cy, 24, 0, Math.PI * 2);
        ctx.fill();
      }
    } else if (bgKey === "neptune" || bgKey === "uranus") {
      ctx.strokeStyle = "rgba(200,240,255,0.15)";
      ctx.lineWidth = 2;
      for (var j = 0; j < 8; j++) {
        ctx.beginPath();
        var x0 = ((j * 140 + sc * 0.2) % (W + 100)) - 50;
        ctx.moveTo(x0, 120 + j * 15);
        ctx.bezierCurveTo(
          x0 + 60,
          80 + j * 10,
          x0 + 120,
          160,
          x0 + 200,
          100 + j * 12
        );
        ctx.stroke();
      }
    } else if (bgKey === "jupiter") {
      var gx = ctx.createRadialGradient(
        W * 0.35 + Math.sin(t) * 20,
        160,
        10,
        W * 0.5,
        200,
        180
      );
      gx.addColorStop(0, "rgba(255,200,120,0.2)");
      gx.addColorStop(1, "rgba(255,120,40,0)");
      ctx.fillStyle = gx;
      ctx.fillRect(0, 40, W, 260);
    }
    ctx.restore();
  }

  function drawGroundLayer(ctx, bgKey, t) {
    var sc = t * 60;
    var top = GROUND_Y;
    var g = ctx.createLinearGradient(0, top, 0, H);
    if (bgKey === "moon" || bgKey === "pluto") {
      g.addColorStop(0, "#4a4a58");
      g.addColorStop(1, "#1a1a24");
    } else if (bgKey === "mars") {
      g.addColorStop(0, "#8b4030");
      g.addColorStop(1, "#301008");
    } else if (bgKey === "m78") {
      g.addColorStop(0, "#c8e8ff");
      g.addColorStop(1, "#5090d0");
    } else {
      g.addColorStop(0, "#3a5040");
      g.addColorStop(1, "#1a2818");
    }
    ctx.fillStyle = g;
    ctx.fillRect(0, top, W, H - top);

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (var x = 0; x < W + 60; x += 36) {
      var ox = (x - (sc % 36)) | 0;
      ctx.beginPath();
      ctx.moveTo(ox, top);
      ctx.lineTo(ox - 14, H);
      ctx.stroke();
    }
    ctx.fillStyle = "rgba(255,255,255,0.04)";
    for (var k = 0; k < 25; k++) {
      ctx.fillRect(
        ((k * 67 + sc * 0.8) % (W + 40)) - 20,
        top + 10 + (k % 5) * 18,
        24,
        4
      );
    }
    ctx.restore();

    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(0, top + 1);
    ctx.lineTo(W, top + 1);
    ctx.stroke();

    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    var glow = ctx.createLinearGradient(0, top - 30, 0, top + 20);
    glow.addColorStop(0, "rgba(255,255,200,0)");
    glow.addColorStop(1, "rgba(255,255,255,0.08)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, top - 30, W, 50);
    ctx.restore();
  }

  function drawBackground(ctx, bgKey, t) {
    ctx.fillStyle = skyGradient(ctx, bgKey);
    ctx.fillRect(0, 0, W, H);
    drawParallaxStars(ctx, bgKey, t);
    drawFarSilhouette(ctx, bgKey, t);
    drawMidClouds(ctx, bgKey, t);
    drawGroundLayer(ctx, bgKey, t);
  }

  /** 赛罗 / 迪迦：计时器灯、胸甲、跑步摆腿、无敌光晕 */
  function drawHero(ctx, p, name, t) {
    var x = p.x;
    var y = p.y;
    var f = p.facing;
    var w = p.w;
    var h = p.h;
    var run = p.runPhase || 0;
    var lamp = 0.65 + 0.35 * Math.sin(t * 8);

    ctx.save();
    ctx.translate(x + w / 2, y + h / 2);
    if (!f) ctx.scale(-1, 1);

    ctx.fillStyle = "rgba(0,0,0,0.35)";
    ctx.beginPath();
    ctx.ellipse(0, h / 2 - 2, 14, 5, 0, 0, Math.PI * 2);
    ctx.fill();

    if (p.invuln > 0 && Math.floor(p.invuln * 12) % 2 === 0) {
      ctx.strokeStyle = "rgba(100,200,255,0.85)";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(0, -4, 26 + Math.sin(t * 14) * 3, 0, Math.PI * 2);
      ctx.stroke();
    }

    var leg = Math.sin(run * 0.018) * 6;
    ctx.fillStyle = "#37474f";
    ctx.fillRect(-8 + leg, 8, 7, 14);
    ctx.fillRect(1 - leg, 8, 7, 14);

    var bodyG = ctx.createLinearGradient(-12, -24, 12, 16);
    if (name === "zero") {
      bodyG.addColorStop(0, "#eceff1");
      bodyG.addColorStop(0.45, "#b0bec5");
      bodyG.addColorStop(1, "#90a4ae");
    } else {
      bodyG.addColorStop(0, "#fafafa");
      bodyG.addColorStop(0.5, "#cfd8dc");
      bodyG.addColorStop(1, "#b0bec5");
    }
    ctx.fillStyle = bodyG;
    ctx.fillRect(-11, -20, 22, 30);

    if (name === "zero") {
      ctx.fillStyle = "#1565c0";
      ctx.fillRect(-13, -10, 26, 12);
      ctx.fillStyle = "#c62828";
      ctx.fillRect(-9, 2, 18, 14);
    } else {
      ctx.fillStyle = "#6a1b9a";
      ctx.fillRect(-13, -10, 26, 14);
      ctx.fillStyle = "#c62828";
      ctx.fillRect(-9, 4, 18, 12);
    }

    ctx.fillStyle = "#ffee58";
    ctx.fillRect(-13, -28, 26, 10);
    ctx.fillStyle = "rgba(255,255,200," + lamp + ")";
    ctx.beginPath();
    ctx.arc(-5, -22, 3, 0, Math.PI * 2);
    ctx.arc(5, -22, 3, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "#fff9c4";
    ctx.beginPath();
    ctx.ellipse(0, -32, 6, 8, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#ffeb3b";
    ctx.beginPath();
    ctx.ellipse(0, -32, 3, 5, 0, 0, Math.PI * 2);
    ctx.fill();

    var armOut = (p.shootFx || 0) > 0;
    ctx.strokeStyle = "#cfd8dc";
    ctx.lineWidth = 4;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(12, -6);
    ctx.lineTo(armOut ? 26 : 18, 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-12, -6);
    ctx.lineTo(-18, 4);
    ctx.stroke();

    if (p.skillGlow && p.skillGlow > 0) {
      ctx.globalCompositeOperation = "lighter";
      var rg = ctx.createRadialGradient(0, -8, 4, 0, -8, 40);
      rg.addColorStop(
        0,
        name === "zero"
          ? "rgba(0,200,255,0.9)"
          : "rgba(255,200,100,0.9)"
      );
      rg.addColorStop(1, "rgba(255,255,255,0)");
      ctx.fillStyle = rg;
      ctx.beginPath();
      ctx.arc(0, -8, 38, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalCompositeOperation = "source-over";
    }

    ctx.restore();
  }

  function drawBoss(ctx, b, label, t) {
    var cx = b.x + b.w / 2;
    var cy = b.y + b.h / 2;
    var pulse = 1 + Math.sin(t * 3) * 0.04;
    var flash = b.hitFlash > 0 ? 1 : 0;

    ctx.save();
    ctx.translate(cx, cy);
    ctx.scale(pulse, pulse);

    ctx.globalCompositeOperation = "lighter";
    var ar = ctx.createRadialGradient(0, 0, 20, 0, 0, b.w * 0.9);
    ar.addColorStop(0, "rgba(255,80,120,0.35)");
    ar.addColorStop(1, "rgba(80,0,60,0)");
    ctx.fillStyle = ar;
    ctx.beginPath();
    ctx.arc(0, 0, b.w, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalCompositeOperation = "source-over";

    var body = ctx.createLinearGradient(-b.w / 2, -b.h / 2, b.w / 2, b.h / 2);
    var bc = b.extraColor || "#4a148c";
    body.addColorStop(0, flash ? "#ffffff" : bc);
    body.addColorStop(0.5, "#2a0a30");
    body.addColorStop(1, bc);
    ctx.fillStyle = body;
    ctx.fillRect(-b.w / 2, -b.h / 2, b.w, b.h);

    if (b.key === "belial") {
      ctx.fillStyle = "#b71c1c";
      ctx.beginPath();
      ctx.moveTo(-b.w / 2 - 6, -b.h / 2 + 10);
      ctx.lineTo(-b.w / 2 - 22, -b.h / 2 - 8);
      ctx.lineTo(-b.w / 2 + 4, -b.h / 2 + 4);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(b.w / 2 + 6, -b.h / 2 + 10);
      ctx.lineTo(b.w / 2 + 22, -b.h / 2 - 8);
      ctx.lineTo(b.w / 2 - 4, -b.h / 2 + 4);
      ctx.fill();
      ctx.strokeStyle = "rgba(255,50,50,0.8)";
      ctx.lineWidth = 3;
      ctx.strokeRect(-b.w / 2, -b.h / 2, b.w, b.h);
    }

    var eyeG = ctx.createRadialGradient(-12, -b.h / 5, 2, -12, -b.h / 5, 9);
    eyeG.addColorStop(0, "#fff");
    eyeG.addColorStop(0.4, "#ff1744");
    eyeG.addColorStop(1, "#300");
    ctx.fillStyle = eyeG;
    ctx.beginPath();
    ctx.arc(-14, -b.h / 5, 7, 0, Math.PI * 2);
    ctx.arc(14, -b.h / 5, 7, 0, Math.PI * 2);
    ctx.fill();

    ctx.fillStyle = "rgba(0,0,0,0.35)";
    ctx.fillRect(-b.w / 2 + 8, b.h / 2 - 18, b.w - 16, 10);

    ctx.fillStyle = "#fff";
    ctx.font = "bold 13px Microsoft YaHei";
    ctx.textAlign = "center";
    ctx.fillText(label, 0, b.h / 2 + 26);
    ctx.restore();
  }

  function makePlayer(ix, name, spawnX) {
    return {
      ix: ix,
      name: name,
      x: spawnX,
      y: GROUND_Y - 48,
      w: 28,
      h: 48,
      vx: 0,
      vy: 0,
      onGround: true,
      wasOnGround: true,
      hp: 5,
      maxHp: 5,
      facing: true,
      shootCd: 0,
      skillCd: 0,
      invuln: 0,
      alive: true,
      runPhase: 0,
      shootFx: 0,
      skillGlow: 0,
    };
  }

  function resetPlayers(players) {
    players[0].x = 120;
    players[0].y = GROUND_Y - 48;
    players[0].vx = players[0].vy = 0;
    players[0].hp = players[0].maxHp;
    players[0].alive = true;
    players[0].invuln = 0;
    players[0].skillCd = 0;
    players[0].shootFx = 0;
    players[0].skillGlow = 0;
    players[1].x = 220;
    players[1].y = GROUND_Y - 48;
    players[1].vx = players[1].vy = 0;
    players[1].hp = players[1].maxHp;
    players[1].alive = true;
    players[1].invuln = 0;
    players[1].skillCd = 0;
    players[1].shootFx = 0;
    players[1].skillGlow = 0;
  }

  function updatePlayer(p, inp, dt, FX) {
    if (!p.alive) return;
    p.wasOnGround = p.onGround;
    var speed = 220;
    var jump = 380;
    if (inp.left) {
      p.vx = -speed;
      p.facing = false;
    } else if (inp.right) {
      p.vx = speed;
      p.facing = true;
    } else {
      p.vx *= Math.pow(0.2, dt);
    }
    if (inp.up && p.onGround) {
      p.vy = -jump;
      p.onGround = false;
      FX.burst(p.x + p.w / 2, p.y + p.h, 8, 200, 230, 255, 40, 140);
    }
    p.vy += 980 * dt;
    p.x += p.vx * dt;
    p.y += p.vy * dt;
    if (p.x < 0) p.x = 0;
    if (p.x + p.w > W) p.x = W - p.w;
    if (p.y + p.h >= GROUND_Y) {
      p.y = GROUND_Y - p.h;
      p.vy = 0;
      p.onGround = true;
      if (!p.wasOnGround) {
        FX.landDust(p.x + p.w / 2, GROUND_Y);
      }
    }
    if (Math.abs(p.vx) > 30 && p.onGround) {
      p.runPhase += Math.abs(p.vx) * dt;
    }
    if (p.shootCd > 0) p.shootCd -= dt;
    if (p.skillCd > 0) p.skillCd -= dt;
    if (p.shootFx > 0) p.shootFx -= dt * 3;
    if (p.skillGlow > 0) p.skillGlow -= dt * 1.2;
    if (p.invuln > 0) p.invuln -= dt;
  }

  function tryShoot(p, bullets, side, FX) {
    if (!p.alive || p.shootCd > 0) return;
    if (!side) return;
    p.shootCd = 0.16;
    p.shootFx = 0.22;
    var bx = p.facing ? p.x + p.w : p.x - 8;
    var by = p.y + 14;
    var dir = p.facing ? 1 : -1;
    var mx = p.facing ? p.x + p.w + 4 : p.x - 4;
    var my = p.y + 16;
    if (p.name === "zero") {
      FX.muzzleFlash(mx, my, dir, 0, 200, 255);
    } else {
      FX.muzzleFlash(mx, my, dir, 255, 200, 120);
    }
    bullets.push({
      x: bx,
      y: by,
      w: 16,
      h: 7,
      vx: p.facing ? 560 : -560,
      vy: 0,
      dmg: 1,
      from: p.ix,
      life: 1.15,
      trail: [],
      hue: p.name === "zero" ? "zero" : "tiga",
    });
  }

  function trySkillZero(p, beams, FX) {
    if (!p.alive || p.skillCd > 0) return false;
    p.skillCd = 2.6;
    p.skillGlow = 0.45;
    var fw = p.facing ? 1 : -1;
    var bw = 400;
    var bx = p.facing ? p.x + p.w - 10 : p.x - bw + 10;
    var by = p.y + 2;
    beams.push({
      kind: "zero",
      x: bx,
      y: by,
      w: bw,
      h: 46,
      life: 0.48,
      tick: 0,
      from: p.ix,
    });
    FX.addShake(5, 0.14);
    FX.ring(p.x + p.w / 2 + fw * 30, p.y + 12, 0, 200, 255);
    FX.beamSparks(bx + bw * 0.2, by, bw * 0.6, 46);
    return true;
  }

  function trySkillTiga(p, beams, FX) {
    if (!p.alive || p.skillCd > 0) return false;
    p.skillCd = 3.0;
    p.skillGlow = 0.5;
    var fw = p.facing ? 1 : -1;
    beams.push({
      kind: "tiga",
      x: p.x + p.w / 2 + fw * 24,
      y: p.y + 14,
      vx: fw * 320,
      vy: 0,
      r: 26,
      life: 0.92,
      hitDb: {},
      age: 0,
    });
    FX.addShake(4, 0.12);
    FX.ring(p.x + p.w / 2, p.y + 14, 255, 220, 100);
    FX.burst(p.x + p.w / 2 + fw * 20, p.y + 14, 20, 255, 220, 150, 60, 200);
    return true;
  }

  function applyZeroBeamDamage(beam, enemies, boss, FX) {
    var rect = { x: beam.x, y: beam.y, w: beam.w, h: beam.h };
    for (var ei = enemies.length - 1; ei >= 0; ei--) {
      var e = enemies[ei];
      if (U.rectsOverlap(rect, e)) {
        e.hp -= 5;
        FX.hitSparks(e.x + e.w / 2, e.y + e.h / 3);
        if (e.hp <= 0) enemies.splice(ei, 1);
      }
    }
    if (boss && boss.alive && U.rectsOverlap(rect, boss)) {
      boss.hp -= 2;
      boss.hitFlash = 8;
      FX.hitSparks(boss.x + boss.w / 2, boss.y + 30);
      FX.addShake(4, 0.1);
      if (boss.hp <= 0) {
        boss.hp = 0;
        boss.alive = false;
      }
    }
  }

  function applyTigaOrb(orb, enemies, boss, dt, FX) {
    orb.age += dt;
    var rw = orb.r * 2;
    var rect = { x: orb.x - orb.r, y: orb.y - orb.r, w: rw, h: rw };
    for (var ei = enemies.length - 1; ei >= 0; ei--) {
      var e = enemies[ei];
      if (!U.rectsOverlap(rect, e)) continue;
      var k = "m" + e.uid;
      if (orb.hitDb[k] !== undefined && orb.age - orb.hitDb[k] < 0.15) continue;
      orb.hitDb[k] = orb.age;
      e.hp -= 3;
      FX.hitSparks(e.x + e.w / 2, e.y + e.h / 3);
      if (e.hp <= 0) enemies.splice(ei, 1);
    }
    if (boss && boss.alive && U.rectsOverlap(rect, boss)) {
      if (orb.hitDb.boss === undefined || orb.age - orb.hitDb.boss >= 0.14) {
        orb.hitDb.boss = orb.age;
        boss.hp -= 3;
        boss.hitFlash = 8;
        FX.hitSparks(boss.x + boss.w / 2, boss.y + 40);
      }
      if (boss.hp <= 0) {
        boss.hp = 0;
        boss.alive = false;
      }
    }
  }

  function updateBeams(beams, enemies, boss, dt, FX) {
    for (var i = beams.length - 1; i >= 0; i--) {
      var b = beams[i];
      b.life -= dt;
      if (b.kind === "zero") {
        b.tick = (b.tick || 0) + dt;
        if (b.tick >= 0.12) {
          b.tick = 0;
          applyZeroBeamDamage(b, enemies, boss, FX);
        }
        if (Math.random() < 0.55) {
          FX.burst(
            b.x + Math.random() * b.w,
            b.y + Math.random() * b.h,
            3,
            100,
            220,
            255,
            30,
            120
          );
        }
      } else if (b.kind === "tiga") {
        b.age = (b.age || 0) + dt;
        b.x += b.vx * dt;
        b.vy = Math.sin(b.age * 11) * 55;
        b.y += b.vy * dt;
        applyTigaOrb(b, enemies, boss, dt, FX);
        if (Math.random() < 0.35) {
          FX.spawnParticle({
            x: b.x + U.rand(-6, 6),
            y: b.y + U.rand(-6, 6),
            vx: U.rand(-40, 40),
            vy: U.rand(-20, 20),
            life: 0.25,
            max: 0.25,
            r: 255,
            g: 230,
            b: 120,
            size: U.rand(2, 5),
            drag: 0.92,
            grav: 0,
          });
        }
      }
      if (b.life <= 0) beams.splice(i, 1);
    }
  }

  function drawBeams(ctx, beams, t) {
    for (var i = 0; i < beams.length; i++) {
      var b = beams[i];
      if (b.kind === "zero") {
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        var g = ctx.createLinearGradient(b.x, b.y, b.x + b.w, b.y);
        g.addColorStop(0, "rgba(0,255,255,0.95)");
        g.addColorStop(0.35, "rgba(100,200,255,0.75)");
        g.addColorStop(0.6, "rgba(255,255,255,0.5)");
        g.addColorStop(1, "rgba(0,180,255,0)");
        ctx.fillStyle = g;
        ctx.fillRect(b.x, b.y, b.w, b.h);
        ctx.globalAlpha = 0.4;
        ctx.fillStyle = "#fff";
        ctx.fillRect(b.x, b.y + b.h * 0.35, b.w * 0.85, b.h * 0.2);
        ctx.restore();
      } else if (b.kind === "tiga") {
        ctx.save();
        ctx.globalCompositeOperation = "lighter";
        var rg = ctx.createRadialGradient(b.x, b.y, 4, b.x, b.y, b.r * 1.8);
        rg.addColorStop(0, "rgba(255,255,220,0.95)");
        rg.addColorStop(0.35, "rgba(255,200,80,0.65)");
        rg.addColorStop(0.7, "rgba(255,120,40,0.35)");
        rg.addColorStop(1, "rgba(255,80,0,0)");
        ctx.fillStyle = rg;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r * 1.6, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "rgba(255,255,200,0.8)";
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.arc(b.x, b.y, b.r, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  function updateBullets(bullets, dt) {
    for (var i = bullets.length - 1; i >= 0; i--) {
      var b = bullets[i];
      b.x += b.vx * dt;
      b.y += b.vy * dt;
      b.life -= dt;
      b._trailT = (b._trailT || 0) + dt;
      if (b._trailT > 0.028) {
        b._trailT = 0;
        if (!b.trail) b.trail = [];
        b.trail.push({ x: b.x + b.w / 2, y: b.y + b.h / 2, a: 0.85 });
        if (b.trail.length > 10) b.trail.shift();
      }
      for (var j = 0; b.trail && j < b.trail.length; j++) {
        b.trail[j].a -= dt * 2.2;
      }
      if (b.life <= 0 || b.x < -40 || b.x > W + 40) bullets.splice(i, 1);
    }
  }

  var nextEnemyUid = 1;
  function spawnGrunt(enemies, level, fromRight) {
    var e = {
      uid: nextEnemyUid++,
      x: fromRight ? W + 20 : -40,
      y: GROUND_Y - 40,
      w: 32,
      h: 40,
      vx: fromRight ? -level.gruntSpeed : level.gruntSpeed,
      hp: level.gruntHp,
      maxHp: level.gruntHp,
      dmgCd: 0,
    };
    enemies.push(e);
  }

  function updateEnemies(enemies, players, dt) {
    for (var i = enemies.length - 1; i >= 0; i--) {
      var e = enemies[i];
      e.x += e.vx * dt;
      if (e.x < -60 || e.x > W + 60) {
        enemies.splice(i, 1);
        continue;
      }
      if (e.dmgCd > 0) e.dmgCd -= dt;
      for (var pi = 0; pi < players.length; pi++) {
        var p = players[pi];
        if (!p.alive) continue;
        if (
          e.dmgCd <= 0 &&
          U.rectsOverlap(
            { x: p.x + 4, y: p.y + 4, w: p.w - 8, h: p.h - 8 },
            { x: e.x + 4, y: e.y + 4, w: e.w - 8, h: e.h - 8 }
          )
        ) {
          if (p.invuln <= 0) {
            p.hp -= 1;
            p.invuln = 1.2;
            if (p.hp <= 0) {
              p.hp = 0;
              p.alive = false;
            }
          }
          e.dmgCd = 0.8;
        }
      }
    }
  }

  function bulletsVsEnemies(bullets, enemies, boss, FX) {
    for (var bi = bullets.length - 1; bi >= 0; bi--) {
      var b = bullets[bi];
      var hit = false;
      for (var ei = enemies.length - 1; ei >= 0; ei--) {
        var e = enemies[ei];
        if (U.rectsOverlap(b, e)) {
          e.hp -= b.dmg;
          hit = true;
          FX.burst(e.x + e.w / 2, e.y + e.h / 2, 6, 255, 200, 80, 40, 180);
          if (e.hp <= 0) {
            FX.hitSparks(e.x + e.w / 2, e.y + e.h / 2);
            enemies.splice(ei, 1);
          }
          break;
        }
      }
      if (!hit && boss && boss.alive && U.rectsOverlap(b, boss)) {
        boss.hp -= b.dmg;
        boss.hitFlash = 6;
        hit = true;
        FX.burst(boss.x + boss.w / 2, boss.y + 25, 8, 255, 100, 120, 30, 160);
        if (boss.hp <= 0) {
          boss.hp = 0;
          boss.alive = false;
        }
      }
      if (hit) bullets.splice(bi, 1);
    }
  }

  function makeBoss(level) {
    return {
      x: W - 200,
      y: GROUND_Y - 120,
      w: 100,
      h: 120,
      vx: 0,
      vy: 0,
      hp: level.bossHp,
      maxHp: level.bossHp,
      alive: true,
      key: level.bossKey,
      name: level.bossName,
      t: 0,
      hitFlash: 0,
      phase: 0,
      extraColor: null,
      shootCd: 0,
    };
  }

  function updateBoss(boss, players, bullets, eBullets, dt, level, FX) {
    if (!boss || !boss.alive) return;
    boss.t += dt;
    if (boss.hitFlash > 0) boss.hitFlash -= dt;

    var leftBound = W * 0.45;
    var targetY = GROUND_Y - boss.h;

    function hurtPlayersInRect(rx, ry, rw, rh, dmg) {
      for (var i = 0; i < players.length; i++) {
        var p = players[i];
        if (!p.alive) continue;
        if (U.rectsOverlap(p, { x: rx, y: ry, w: rw, h: rh })) {
          if (p.invuln <= 0) {
            p.hp -= dmg;
            p.invuln = 1.0;
            if (p.hp <= 0) {
              p.hp = 0;
              p.alive = false;
            }
          }
        }
      }
    }

    function bossShoot(dx, dy) {
      eBullets.push({
        x: boss.x + boss.w / 2,
        y: boss.y + boss.h / 2,
        w: 14,
        h: 14,
        vx: dx,
        vy: dy,
        life: 3,
        trail: [],
        _tr: 0,
      });
    }

    /* 通用横向徘徊 */
    var sway = Math.sin(boss.t * 1.2) * 90;
    boss.x = U.clamp(leftBound + sway, leftBound - 20, W - boss.w - 10);
    boss.y += (targetY - boss.y) * Math.min(1, dt * 4);

    var k = boss.key;

    if (k === "redking") {
      boss._stompT = (boss._stompT || 0) + dt;
      if (boss._stompT > 2.2) {
        boss._stompT = 0;
        hurtPlayersInRect(boss.x - 40, boss.y + 60, boss.w + 80, 30, 1);
        FX.addShake(9, 0.16);
        FX.burst(boss.x + boss.w / 2, GROUND_Y - 10, 22, 180, 140, 100, 60, 220);
      }
    } else if (k === "black") {
      boss.shootCd -= dt;
      if (boss.shootCd <= 0) {
        boss.shootCd = 0.55;
        var tgt =
          players[0].alive ? players[0] : players[1];
        if (!tgt.alive) tgt = players[1];
        var px = tgt.x + tgt.w / 2;
        var py = tgt.y + tgt.h / 2;
        var ang = Math.atan2(py - (boss.y + boss.h / 2), px - (boss.x + boss.w / 2));
        bossShoot(Math.cos(ang) * 200, Math.sin(ang) * 200);
      }
    } else if (k === "golza") {
      boss.extraColor = "#5d4037";
      boss._quakeT = (boss._quakeT || 0) + dt;
      if (boss._quakeT > 2.8) {
        boss._quakeT = 0;
        hurtPlayersInRect(0, GROUND_Y - 28, W, 36, 1);
        FX.addShake(12, 0.2);
        FX.burst(W * 0.5, GROUND_Y - 20, 30, 160, 120, 80, 80, 260);
      }
    } else if (k === "temperor") {
      boss.extraColor = "#283593";
      boss.shootCd -= dt;
      if (boss.shootCd <= 0) {
        boss.shootCd = 0.35;
        bossShoot(-220, U.rand(-40, 40));
        bossShoot(-200, U.rand(-80, 80));
      }
    } else if (k === "baltan") {
      boss.extraColor = "#00695c";
    } else if (k === "bogun") {
      boss.extraColor = "#4fc3f7";
      boss.vy = Math.sin(boss.t * 3) * 60;
      boss.y = U.clamp(boss.y + boss.vy * dt, GROUND_Y - 200, GROUND_Y - boss.h);
    } else if (k === "metron") {
      boss.extraColor = "#ff6f00";
      if (boss.t % 3 < 0.15) {
        for (var j = 0; j < 5; j++)
          bossShoot(-150, -80 + j * 40);
      }
    } else if (k === "twin") {
      boss.extraColor = "#b71c1c";
      boss.w = 110;
      boss._breathT = (boss._breathT || 0) + dt;
      if (boss._breathT > 1.4) {
        boss._breathT = 0;
        hurtPlayersInRect(boss.x, boss.y + 40, boss.w, 50, 1);
      }
    } else if (k === "emperor") {
      boss.extraColor = "#212121";
      boss.shootCd -= dt;
      if (boss.shootCd <= 0) {
        boss.shootCd = 0.28;
        bossShoot(-260, Math.sin(boss.t * 5) * 120);
      }
      if (boss.hp < boss.maxHp * 0.4) {
        boss._fieldT = (boss._fieldT || 0) + dt;
        if (boss._fieldT > 2.0) {
          boss._fieldT = 0;
          hurtPlayersInRect(0, 100, W, 220, 1);
        }
      }
    } else if (k === "belial") {
      boss.extraColor = "#1a0000";
      boss.w = 120;
      boss.h = 130;
      /* 贝利亚：多段弹幕 + 冲刺 */
      boss.shootCd -= dt;
      if (boss.shootCd <= 0) {
        boss.shootCd = boss.phase === 1 ? 0.22 : 0.35;
        for (var n = 0; n < 7; n++)
          bossShoot(-200, -120 + n * 40);
      }
      if (boss.hp < boss.maxHp * 0.55) boss.phase = 1;
      if (boss.t % 4 < 0.25) {
        hurtPlayersInRect(0, GROUND_Y - 50, W * 0.6, 45, 1);
        FX.addShake(7, 0.14);
        FX.burst(W * 0.25, GROUND_Y - 30, 16, 200, 40, 40, 100, 280);
      }
    }
  }

  function updateEnemyBullets(eBullets, players, dt) {
    for (var i = eBullets.length - 1; i >= 0; i--) {
      var eb = eBullets[i];
      eb.x += eb.vx * dt;
      eb.y += eb.vy * dt;
      eb.life -= dt;
      eb._tr = (eb._tr || 0) + dt;
      if (eb._tr > 0.035) {
        eb._tr = 0;
        if (!eb.trail) eb.trail = [];
        eb.trail.push({ x: eb.x, y: eb.y, a: 0.7 });
        if (eb.trail.length > 12) eb.trail.shift();
      }
      for (var ti = 0; eb.trail && ti < eb.trail.length; ti++) {
        eb.trail[ti].a -= dt * 1.8;
      }
      if (eb.life <= 0 || eb.x < -50) {
        eBullets.splice(i, 1);
        continue;
      }
      for (var pi = 0; pi < players.length; pi++) {
        var p = players[pi];
        if (!p.alive) continue;
        if (U.rectsOverlap(eb, p) && p.invuln <= 0) {
          p.hp -= 1;
          p.invuln = 0.9;
          if (p.hp <= 0) {
            p.hp = 0;
            p.alive = false;
          }
          eBullets.splice(i, 1);
          break;
        }
      }
    }
  }

  function drawEnemies(ctx, enemies, t) {
    for (var i = 0; i < enemies.length; i++) {
      var e = enemies[i];
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      var ag = ctx.createRadialGradient(
        e.x + e.w / 2,
        e.y + e.h / 2,
        4,
        e.x + e.w / 2,
        e.y + e.h / 2,
        e.w
      );
      ag.addColorStop(0, "rgba(255,80,180,0.45)");
      ag.addColorStop(1, "rgba(60,0,80,0)");
      ctx.fillStyle = ag;
      ctx.fillRect(e.x - 4, e.y - 4, e.w + 8, e.h + 8);
      ctx.restore();

      var bob = Math.sin(t * 6 + i) * 2;
      var g = ctx.createLinearGradient(e.x, e.y, e.x + e.w, e.y + e.h);
      g.addColorStop(0, "#6a1b9a");
      g.addColorStop(0.5, "#4a148c");
      g.addColorStop(1, "#38006b");
      ctx.fillStyle = g;
      ctx.fillRect(e.x, e.y + bob, e.w, e.h);

      ctx.fillStyle = "#ffeb3b";
      var ex = 0.35 + 0.15 * Math.sin(t * 12 + i);
      ctx.globalAlpha = ex;
      ctx.beginPath();
      ctx.arc(e.x + 10, e.y + 14 + bob, 5, 0, Math.PI * 2);
      ctx.arc(e.x + 22, e.y + 14 + bob, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = "rgba(255,0,100,0.5)";
      ctx.lineWidth = 2;
      ctx.strokeRect(e.x, e.y + bob, e.w, e.h);
    }
  }

  function drawBullets(ctx, bullets, eBullets, t) {
    for (var i = 0; i < bullets.length; i++) {
      var b = bullets[i];
      if (b.trail) {
        for (var j = 0; j < b.trail.length; j++) {
          var tr = b.trail[j];
          if (tr.a <= 0) continue;
          ctx.globalAlpha = tr.a * 0.5;
          ctx.fillStyle =
            b.hue === "tiga" ? "rgba(255,200,120,0.9)" : "rgba(0,230,255,0.9)";
          ctx.beginPath();
          ctx.arc(tr.x, tr.y, 4 + j * 0.2, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.globalAlpha = 1;
      }
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      var cg = ctx.createLinearGradient(b.x, b.y, b.x + b.w, b.y + b.h);
      if (b.hue === "tiga") {
        cg.addColorStop(0, "#fff8e1");
        cg.addColorStop(0.5, "#ffab40");
        cg.addColorStop(1, "#ff6d00");
      } else {
        cg.addColorStop(0, "#e0f7ff");
        cg.addColorStop(0.45, "#00e5ff");
        cg.addColorStop(1, "#0091ea");
      }
      ctx.fillStyle = cg;
      ctx.fillRect(b.x, b.y, b.w, b.h);
      ctx.fillStyle = "rgba(255,255,255,0.9)";
      ctx.fillRect(b.x + b.w * 0.2, b.y + 1, b.w * 0.35, b.h - 2);
      ctx.restore();
    }
    for (var k = 0; k < eBullets.length; k++) {
      var e = eBullets[k];
      if (e.trail) {
        for (var m = 0; m < e.trail.length; m++) {
          var et = e.trail[m];
          if (et.a <= 0) continue;
          ctx.globalAlpha = Math.max(0, et.a) * 0.45;
          ctx.fillStyle = "#ff1744";
          ctx.beginPath();
          ctx.arc(et.x, et.y, 10, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.globalAlpha = 1;
      }
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      var rg = ctx.createRadialGradient(e.x, e.y, 2, e.x, e.y, 14);
      rg.addColorStop(0, "#fff");
      rg.addColorStop(0.35, "#ff4081");
      rg.addColorStop(1, "rgba(120,0,40,0)");
      ctx.fillStyle = rg;
      ctx.beginPath();
      ctx.arc(e.x, e.y, 12, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }

  function drawHUD(ctx, level, waveIndex, players, boss, state) {
    ctx.fillStyle = "rgba(0,0,0,0.45)";
    ctx.fillRect(0, 0, W, 52);
    ctx.fillStyle = "#fff";
    ctx.font = "14px Microsoft YaHei";
    ctx.textAlign = "left";
    var waveTxt =
      state === "boss"
        ? "BOSS 决战"
        : "清剿波次 " + (waveIndex + 1) + "/" + level.waveCount;
    ctx.fillText(level.title + "  " + level.subtitle + "   " + waveTxt, 16, 22);
    ctx.font = "12px Microsoft YaHei";
    ctx.fillStyle = "#b3e5fc";
    ctx.fillText(
      "P1: A/D W 跳 | K 普攻 | L 集束射线    P2: 方向键跳 | O/小键盘0 普攻 | P/; 哉佩利敖",
      16,
      42
    );

    var hx = W - 280;
    for (var i = 0; i < players.length; i++) {
      var p = players[i];
      var label = i === 0 ? "赛罗" : "迪迦";
      ctx.fillStyle = p.alive ? "#fff" : "#666";
      ctx.fillText(label + " HP", hx + i * 130, 22);
      for (var h = 0; h < p.maxHp; h++) {
        ctx.fillStyle =
          h < p.hp ? (i === 0 ? "#448aff" : "#ab47bc") : "#333";
        ctx.fillRect(hx + i * 130 + h * 18, 28, 14, 10);
      }
    }

    if (boss && boss.alive && state === "boss") {
      var bw = 300;
      var bx = (W - bw) / 2;
      ctx.fillStyle = "rgba(0,0,0,0.5)";
      ctx.fillRect(bx, H - 36, bw, 22);
      ctx.fillStyle = "#ff5252";
      var ratio = boss.hp / boss.maxHp;
      ctx.fillRect(bx + 4, H - 32, (bw - 8) * ratio, 14);
      ctx.fillStyle = "#fff";
      ctx.font = "12px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText("BOSS " + boss.name, bx + bw / 2, H - 40);
      ctx.textAlign = "left";
    }
  }

  function Game(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.state = "title";
    this.levelIndex = 0;
    this.waveIndex = 0;
    this.waveTimer = 0;
    this.spawnTimer = 0;
    this.gruntsToSpawn = 0;
    this.storyAlpha = 0;

    this.players = [
      makePlayer(0, "zero", 120),
      makePlayer(1, "tiga", 220),
    ];
    this.players[0].ix = 0;
    this.players[1].ix = 1;

    this.bullets = [];
    this.eBullets = [];
    this.enemies = [];
    this.beams = [];
    this.boss = null;
    this._prevSkillP1 = false;
    this._prevSkillP2 = false;
  }

  Game.prototype.currentLevel = function () {
    return LEVELS[this.levelIndex];
  };

  Game.prototype.startLevel = function () {
    var lv = this.currentLevel();
    this.waveIndex = 0;
    this.waveTimer = 2;
    this.spawnTimer = 0;
    this.gruntsToSpawn = 3 + this.waveIndex * 2;
    this.bullets.length = 0;
    this.eBullets.length = 0;
    this.enemies.length = 0;
    this.beams.length = 0;
    this.boss = null;
    if (global.FX) global.FX.clear();
    resetPlayers(this.players);
    this.state = "story";
    this.storyAlpha = 0;
  };

  Game.prototype.beginPlay = function () {
    this.state = "waves";
    this.spawnWave();
  };

  Game.prototype.spawnWave = function () {
    var lv = this.currentLevel();
    this.gruntsToSpawn = 4 + this.waveIndex * 2 + Math.floor(lv.id / 3);
    this.spawnTimer = 0.4;
  };

  Game.prototype.startBoss = function () {
    var lv = this.currentLevel();
    this.boss = makeBoss(lv);
    this.state = "boss";
    this.bullets.length = 0;
    this.eBullets.length = 0;
    this.enemies.length = 0;
    this.beams.length = 0;
  };

  Game.prototype.nextLevel = function () {
    this.levelIndex++;
    if (this.levelIndex >= LEVELS.length) {
      this.state = "victory";
    } else {
      this.startLevel();
    }
  };

  Game.prototype.update = function (dt, input) {
    var self = this;
    var lv = this.currentLevel();

    if (this.state === "title") {
      if (input.anyStart) {
        this.levelIndex = 0;
        this.startLevel();
      }
      return;
    }

    if (this.state === "story") {
      this.storyAlpha = U.clamp(this.storyAlpha + dt * 0.8, 0, 1);
      if (this.storyAlpha >= 1 && input.anyStart) this.beginPlay();
      return;
    }

    if (this.state === "levelclear") {
      if (input.anyStart) this.nextLevel();
      return;
    }

    if (this.state === "gameover") {
      if (input.anyStart) {
        this.levelIndex = 0;
        this.startLevel();
      }
      return;
    }

    if (this.state === "victory") {
      if (input.anyStart) {
        this.state = "title";
      }
      return;
    }

    var inp = global.Input.poll();
    var FX = global.FX;

    if (this.state === "waves") {
      this.waveTimer -= dt;
      if (this.waveTimer > 0) {
        FX.update(dt);
        return;
      }

      this.spawnTimer -= dt;
      if (this.spawnTimer <= 0 && this.gruntsToSpawn > 0) {
        this.spawnTimer = lv.spawnInterval * U.rand(0.85, 1.15);
        this.gruntsToSpawn--;
        spawnGrunt(this.enemies, lv, U.randInt(0, 1) === 1);
      }

      updatePlayer(this.players[0], inp.p1, dt, FX);
      updatePlayer(this.players[1], inp.p2, dt, FX);
      tryShoot(this.players[0], this.bullets, inp.p1.shoot, FX);
      tryShoot(this.players[1], this.bullets, inp.p2.shoot, FX);
      if (inp.p1.skill && !this._prevSkillP1) {
        trySkillZero(this.players[0], this.beams, FX);
      }
      if (inp.p2.skill && !this._prevSkillP2) {
        trySkillTiga(this.players[1], this.beams, FX);
      }
      this._prevSkillP1 = !!inp.p1.skill;
      this._prevSkillP2 = !!inp.p2.skill;

      updateBullets(this.bullets, dt);
      updateBeams(this.beams, this.enemies, null, dt, FX);
      updateEnemies(this.enemies, this.players, dt);
      bulletsVsEnemies(this.bullets, this.enemies, null, FX);

      FX.update(dt);

      var anyAlive = this.players[0].alive || this.players[1].alive;
      if (!anyAlive) {
        this.state = "gameover";
        return;
      }

      if (this.gruntsToSpawn <= 0 && this.enemies.length === 0) {
        this.waveIndex++;
        if (this.waveIndex >= lv.waveCount) {
          this.startBoss();
        } else {
          this.waveTimer = 1.8;
          this.spawnWave();
        }
      }
    } else if (this.state === "boss") {
      updatePlayer(this.players[0], inp.p1, dt, FX);
      updatePlayer(this.players[1], inp.p2, dt, FX);
      tryShoot(this.players[0], this.bullets, inp.p1.shoot, FX);
      tryShoot(this.players[1], this.bullets, inp.p2.shoot, FX);
      if (inp.p1.skill && !this._prevSkillP1) {
        trySkillZero(this.players[0], this.beams, FX);
      }
      if (inp.p2.skill && !this._prevSkillP2) {
        trySkillTiga(this.players[1], this.beams, FX);
      }
      this._prevSkillP1 = !!inp.p1.skill;
      this._prevSkillP2 = !!inp.p2.skill;

      updateBullets(this.bullets, dt);
      updateBeams(this.beams, this.enemies, this.boss, dt, FX);
      updateBoss(
        this.boss,
        this.players,
        this.bullets,
        this.eBullets,
        dt,
        lv,
        FX
      );

      /* 巴尔坦分身：周期性刷小怪 */
      if (this.boss && this.boss.key === "baltan" && this.boss.alive) {
        this.boss._spawnT = (this.boss._spawnT || 0) + dt;
        if (this.boss._spawnT > 2.2) {
          this.boss._spawnT = 0;
          spawnGrunt(this.enemies, lv, true);
        }
      }

      updateEnemies(this.enemies, this.players, dt);
      updateEnemyBullets(this.eBullets, this.players, dt);
      bulletsVsEnemies(this.bullets, this.enemies, this.boss, FX);

      FX.update(dt);

      var anyAlive2 = this.players[0].alive || this.players[1].alive;
      if (!anyAlive2) {
        this.state = "gameover";
        return;
      }

      if (this.boss && !this.boss.alive) {
        this.state = "levelclear";
      }
    }
  };

  Game.prototype.draw = function () {
    var ctx = this.ctx;
    var lv = this.levelIndex < LEVELS.length ? this.currentLevel() : LEVELS[LEVELS.length - 1];
    var t = performance.now() * 0.001;
    var sh = global.FX ? global.FX.getShake() : { x: 0, y: 0 };

    if (this.state === "title") {
      drawBackground(ctx, "m78", t);
      ctx.fillStyle = "rgba(0,0,0,0.5)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#ffe082";
      ctx.font = "bold 36px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText("M78 星云营救", W / 2, 160);
      ctx.fillStyle = "#fff";
      ctx.font = "18px Microsoft YaHei";
      ctx.fillText("赛罗 & 迪迦 双人闯关", W / 2, 210);
      ctx.fillStyle = "#b2ebf2";
      ctx.font = "14px Microsoft YaHei";
      ctx.fillText("从地球启程，穿越太阳系，夺回光之国", W / 2, 255);
      ctx.fillStyle = "#ffecb3";
      ctx.fillText("按 空格 / 回车 开始", W / 2, 320);
      ctx.textAlign = "left";
      return;
    }

    if (this.state === "story") {
      drawBackground(ctx, lv.bgKey, t);
      ctx.fillStyle = "rgba(0,0,0," + (0.55 * this.storyAlpha) + ")";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "rgba(255,255,255," + this.storyAlpha + ")";
      ctx.font = "bold 22px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText(lv.title + " — " + lv.subtitle, W / 2, 200);
      ctx.font = "16px Microsoft YaHei";
      var lines = [];
      for (var si = 0; si < lv.story.length; si += 20) {
        lines.push(lv.story.slice(si, si + 20));
      }
      for (var li = 0; li < lines.length; li++) {
        ctx.fillText(lines[li], W / 2, 240 + li * 28);
      }
      ctx.fillStyle = "rgba(255,235,180," + this.storyAlpha + ")";
      ctx.font = "14px Microsoft YaHei";
      ctx.fillText("按 空格 / 回车 出击", W / 2, H - 120);
      ctx.textAlign = "left";
      return;
    }

    if (this.state === "levelclear") {
      drawBackground(ctx, lv.bgKey, t);
      drawEnemies(ctx, this.enemies, t);
      if (this.players[0].alive) drawHero(ctx, this.players[0], "zero", t);
      if (this.players[1].alive) drawHero(ctx, this.players[1], "tiga", t);
      ctx.fillStyle = "rgba(0,0,0,0.55)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#fff59d";
      ctx.font = "bold 28px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText("关卡完成！", W / 2, H / 2 - 20);
      ctx.fillStyle = "#fff";
      ctx.font = "16px Microsoft YaHei";
      ctx.fillText("按 空格 / 回车 前往下一星球", W / 2, H / 2 + 24);
      ctx.textAlign = "left";
      return;
    }

    if (this.state === "gameover") {
      drawBackground(ctx, lv.bgKey, t);
      ctx.fillStyle = "rgba(0,0,0,0.65)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#ef5350";
      ctx.font = "bold 32px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText("任务失败", W / 2, H / 2 - 10);
      ctx.fillStyle = "#fff";
      ctx.font = "16px Microsoft YaHei";
      ctx.fillText("空格 / 回车 从第一关重试", W / 2, H / 2 + 36);
      ctx.textAlign = "left";
      return;
    }

    if (this.state === "victory") {
      drawBackground(ctx, "m78", t);
      ctx.fillStyle = "rgba(0,0,0,0.5)";
      ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#fff176";
      ctx.font = "bold 30px Microsoft YaHei";
      ctx.textAlign = "center";
      ctx.fillText("光之国得救！", W / 2, H / 2 - 40);
      ctx.fillStyle = "#e1f5fe";
      ctx.font = "16px Microsoft YaHei";
      ctx.fillText("贝利亚被击退，等离子火花塔重新闪耀。", W / 2, H / 2);
      ctx.fillText("感谢两位奥特战士！", W / 2, H / 2 + 32);
      ctx.fillStyle = "#ffe082";
      ctx.font = "14px Microsoft YaHei";
      ctx.fillText("空格 / 回车 返回标题", W / 2, H / 2 + 80);
      ctx.textAlign = "left";
      return;
    }

    ctx.save();
    ctx.translate(sh.x, sh.y);
    drawBackground(ctx, lv.bgKey, t);
    drawEnemies(ctx, this.enemies, t);
    drawBeams(ctx, this.beams, t);
    drawBullets(ctx, this.bullets, this.eBullets, t);
    if (this.boss && this.boss.alive) {
      drawBoss(ctx, this.boss, this.boss.name, t);
    }
    if (this.players[0].alive) drawHero(ctx, this.players[0], "zero", t);
    if (this.players[1].alive) drawHero(ctx, this.players[1], "tiga", t);
    if (global.FX) global.FX.draw(ctx);
    ctx.restore();
    drawHUD(ctx, lv, this.waveIndex, this.players, this.boss, this.state);
  };

  function main() {
    var parent = document.getElementById("wrap");
    var canvas = createCanvas(parent);
    global.Input.init();
    var game = new Game(canvas);
    var last = performance.now();

    function frame(now) {
      var dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      var input = global.Input.poll();
      game.update(dt, input);
      game.draw();
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  global.addEventListener("load", main);
})(typeof window !== "undefined" ? window : globalThis);
