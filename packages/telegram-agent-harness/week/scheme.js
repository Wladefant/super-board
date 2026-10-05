// Sets the colour scheme before the first paint, so a light theme never shows the dark one first (Refs #562).
// This is a classic script in <head>, so it runs before the body renders; the module week.js runs only after
// the whole page has parsed. week.js calls weekScheme() again on Telegram's themeChanged and on a system change.
(() => {
  const systemLight = window.matchMedia('(prefers-color-scheme: light)');
  /**
   * Telegram's colorScheme when Telegram sends a theme. Outside Telegram, telegram-web-app.js still loads
   * but sends no theme, so the page follows prefers-color-scheme. Returns whether the theme is Telegram's.
   */
  window.weekScheme = () => {
    const tg = window.Telegram?.WebApp;
    const fromTelegram = Boolean(tg?.themeParams?.bg_color) && (tg.colorScheme === 'light' || tg.colorScheme === 'dark');
    document.documentElement.dataset.scheme = fromTelegram ? tg.colorScheme : systemLight.matches ? 'light' : 'dark';
    return fromTelegram;
  };
  window.weekScheme();
})();
