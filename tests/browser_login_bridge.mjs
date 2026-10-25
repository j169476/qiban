// Synthetic browser surface for the actual packaged helper. Never visits a website.
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const [bridgePath, ticketPath, scenario] = process.argv.slice(2);
const { stageBrowserLogin } = await import(pathToFileURL(bridgePath).href);
const ticket = JSON.parse(await readFile(ticketPath, "utf8"));
let urlReads = 0;
let cookieReads = 0;
const mapping = { SESSDATA:"NEW_SESSION_123", bili_jct:"NEW_CSRF_123", DedeUserID:"123",
  buvid3:"NEW_DEVICE_3", buvid4:"NEW_DEVICE_4" };
const tab = {
  async url() {
    urlReads++;
    if (scenario === "wrong_origin" || (scenario === "navigated" && urlReads > 1))
      return "https://www.bilibili.com.attacker.invalid/";
    return "https://www.bilibili.com/";
  },
  capabilities: { async get(name) {
    if (name !== "cdp") throw new Error("Unexpected capability");
    return { async send(method) {
      if (method === "Runtime.evaluate") return { result: { value: "NEW_REFRESH_123" } };
      if (method !== "Network.getCookies") throw new Error("Unexpected CDP method");
      cookieReads++;
      const values = { ...mapping };
      if (scenario === "wrong_account") values.DedeUserID = "999";
      if (scenario === "missing_device") delete values.buvid4;
      if (scenario === "changed_session" && cookieReads > 1) values.SESSDATA = "DIFFERENT_SESSION";
      const cookies = Object.entries(values).map(([name,value]) => ({name,value,domain:".bilibili.com"}));
      if (scenario === "conflicting_cookies") cookies.push({name:"SESSDATA",value:"OTHER",domain:"www.bilibili.com"});
      if (scenario === "reordered" && cookieReads > 1) cookies.reverse();
      // Unrelated domains and names must never reach the child process.
      cookies.push({name:"tracking",value:"DO_NOT_EXPORT",domain:".bilibili.com"});
      cookies.push({name:"SESSDATA",value:"OTHER_WEBSITE",domain:".unrelated.invalid"});
      return { cookies };
    } };
  } },
};
try {
  console.log(JSON.stringify(await stageBrowserLogin(tab, ticket)));
} catch (error) {
  console.log(JSON.stringify({error:error.message,cookieReads}));
  process.exitCode = 2;
}
