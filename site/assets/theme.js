// Тема сайта: тёмная по умолчанию, круглая кнопка переключает «тёмная -> светлая -> авто».
// Скрипт подключается в <head> без defer: атрибут data-theme ставится до отрисовки, без вспышки.
(function () {
  var KEY = "demo-stm32-skills-theme";
  var ORDER = ["dark", "light", "auto"];
  var LABELS = { dark: "Тема: тёмная", light: "Тема: светлая", auto: "Тема: как в системе" };
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  var root = document.documentElement;

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return ORDER.indexOf(value) >= 0 ? value : "dark";
    } catch (error) {
      return "dark";
    }
  }

  function save(value) {
    try {
      window.localStorage.setItem(KEY, value);
    } catch (error) {
      // приватный режим или запрет хранилища: тема действует до перезагрузки
    }
  }

  function apply(pref) {
    var theme = pref === "auto" ? (media && media.matches ? "light" : "dark") : pref;
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-theme-pref", pref);
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.setAttribute("aria-label", LABELS[pref] + ". Нажмите, чтобы сменить");
      button.setAttribute("title", LABELS[pref]);
    }
  }

  var pref = stored();
  apply(pref);

  if (media) {
    var onChange = function () {
      if (pref === "auto") apply(pref);
    };
    if (media.addEventListener) media.addEventListener("change", onChange);
    else if (media.addListener) media.addListener(onChange);
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply(pref);
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    button.addEventListener("click", function () {
      pref = ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length];
      save(pref);
      apply(pref);
    });
  });
})();
