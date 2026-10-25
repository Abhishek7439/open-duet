const assert = require("node:assert/strict");
const { test } = require("node:test");
const { validateGoogleAuthorizationUrl, registerGoogleSignIn } = require("../src/google-sign-in");

const origin = "http://127.0.0.1:51234";
const redirect = "http://127.0.0.1:45678/";
const valid = `https://accounts.google.com/o/oauth2/v2/auth?state=secret&redirect_uri=${encodeURIComponent(redirect)}&response_type=code`;

test("opens only Google OAuth URLs with a loopback code redirect", async () => {
  assert.equal(new URL(validateGoogleAuthorizationUrl(valid)).origin, "https://accounts.google.com");
  const opened = [];
  const window = { webContents: { getURL: () => `${origin}/settings/` } };
  let handler;
  registerGoogleSignIn({ ipcMain: { handle: (_channel, fn) => { handler = fn; } }, openExternal: async (url) => opened.push(url), getWindow: () => window, getOrigin: () => origin });
  await handler({ sender: window.webContents }, valid);
  assert.deepEqual(opened, [new URL(valid).href]);
});

test("rejects forged destinations, callback targets, and state parameters", () => {
  for (const raw of [
    valid.replace("accounts.google.com", "accounts.google.com.evil.test"),
    valid.replace(encodeURIComponent(redirect), encodeURIComponent("http://127.0.0.1:80/")),
    `${valid}&state=second`,
    valid.replace("response_type=code", "response_type=token"),
    valid.replace(encodeURIComponent(redirect), encodeURIComponent("http://localhost:45678/")),
  ]) assert.throws(() => validateGoogleAuthorizationUrl(raw));
});

test("rejects IPC calls from a foreign origin or a stale window", async () => {
  for (const { windowUrl, senderIsWindow } of [{ windowUrl: "https://evil.test/settings/", senderIsWindow: true }, { windowUrl: `${origin}/settings/`, senderIsWindow: false }]) {
    const window = { webContents: { getURL: () => windowUrl } };
    const sender = senderIsWindow ? window.webContents : { getURL: () => `${origin}/settings/` };
    let handler;
    const opened = [];
    registerGoogleSignIn({ ipcMain: { handle: (_channel, fn) => { handler = fn; } }, openExternal: async (value) => opened.push(value), getWindow: () => window, getOrigin: () => origin });
    await assert.rejects(handler({ sender }, valid));
    assert.deepEqual(opened, []);
  }
});
