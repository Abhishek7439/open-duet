const { sameOrigin } = require("./origin");

function validateGoogleAuthorizationUrl(raw) {
  if (typeof raw !== "string" || raw.length > 16_384 || /\s/u.test(raw)) {
    throw new Error("Invalid Google sign-in URL.");
  }
  let url;
  try { url = new URL(raw); }
  catch { throw new Error("Invalid Google sign-in URL."); }
  const query = url.searchParams;
  const redirect = query.get("redirect_uri");
  let callback;
  try { callback = new URL(redirect); }
  catch { throw new Error("Invalid Google sign-in URL."); }
  if (
    url.origin !== "https://accounts.google.com" || url.pathname !== "/o/oauth2/v2/auth"
    || url.username || url.password || url.hash
    || query.getAll("state").length !== 1 || !query.get("state") || query.get("state").length > 512
    || /\s/u.test(query.get("state"))
    || query.getAll("redirect_uri").length !== 1
    || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/$/u.test(redirect)
    || callback.port < 1024 || callback.port > 65535
    || query.getAll("response_type").length !== 1 || query.get("response_type") !== "code"
  ) throw new Error("Invalid Google sign-in URL.");
  return url.href;
}

function registerGoogleSignIn({ ipcMain, openExternal, getWindow, getOrigin }) {
  ipcMain.handle("open-dots:google-sign-in", async (event, raw) => {
    const window = getWindow();
    if (!window || event.sender !== window.webContents || !sameOrigin(window.webContents.getURL(), getOrigin())) {
      throw new Error("Google sign-in must be started from Open Duet Settings.");
    }
    await openExternal(validateGoogleAuthorizationUrl(raw));
  });
}

module.exports = { validateGoogleAuthorizationUrl, registerGoogleSignIn };
