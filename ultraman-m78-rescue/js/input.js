(function (global) {
  "use strict";

  var keys = Object.create(null);

  function onDown(e) {
    keys[e.code] = true;
    if (
      [
        "Space",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Tab",
      ].indexOf(e.code) >= 0
    ) {
      e.preventDefault();
    }
  }

  function onUp(e) {
    keys[e.code] = false;
  }

  function init() {
    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup", onUp);
  }

  /** P1: K 射 L 集束射线 | P2: O/小键盘0 射 P 哉佩利敖光线 */
  function poll() {
    return {
      p1: {
        left: !!keys["KeyA"],
        right: !!keys["KeyD"],
        up: !!keys["KeyW"],
        shoot: !!keys["KeyK"],
        skill: !!keys["KeyL"],
      },
      p2: {
        left: !!keys["ArrowLeft"],
        right: !!keys["ArrowRight"],
        up: !!keys["ArrowUp"],
        shoot:
          !!keys["ControlRight"] ||
          !!keys["Numpad0"] ||
          !!keys["KeyO"],
        skill: !!keys["KeyP"] || !!keys["Semicolon"],
      },
      anyStart:
        !!keys["Enter"] ||
        !!keys["Space"] ||
        !!keys["KeyP"],
      pause: !!keys["Escape"],
    };
  }

  global.Input = { init, poll };
})(typeof window !== "undefined" ? window : globalThis);
