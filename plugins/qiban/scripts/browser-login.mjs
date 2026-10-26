// Import from cua_repl after selecting the authorized official Bilibili tab.
// Secrets stay in JS/process memory, then in user-bound Windows ciphertext.
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";

const origin = "https://www.bilibili.com";
const fields = { SESSDATA: "sessdata", bili_jct: "bili_jct", DedeUserID: "dedeuserid",
  buvid3: "buvid3", buvid4: "buvid4", ac_time_value: "ac_time_value" };

function checkOrigin(url) {
  const parsed = new URL(url);
  if (parsed.origin !== origin || parsed.username || parsed.password) {
    throw new Error("请使用已登录的 https://www.bilibili.com 官方标签页");
  }
}

function selectedCookies(response) {
  const result = {};
  for (const cookie of response.cookies ?? []) {
    if (![".bilibili.com", "bilibili.com", "www.bilibili.com"].includes(cookie.domain)) continue;
    const field = fields[cookie.name];
    if (!field || !cookie.value) continue;
    if (result[field] && result[field] !== cookie.value) {
      throw new Error("发现冲突的网页登录凭证，请重新登录后再试");
    }
    result[field] = cookie.value;
  }
  return result;
}

export async function stageBrowserLogin(tab, ticket) {
  if (!/^[a-f0-9]{32}$/.test(ticket?.ticket_id ?? "") || ticket.status !== "awaiting_browser"
      || !/^[0-9]{1,20}$/.test(ticket.expected_uid ?? "")
      || !Number.isFinite(Date.parse(ticket.expires_at)) || Date.parse(ticket.expires_at) <= Date.now()) {
    throw new Error("请先在管理聊天发起一个有效的浏览器登录任务");
  }
  checkOrigin(await tab.url());
  const runtime = JSON.parse(await readFile(new URL("../runtime.json", import.meta.url), "utf8"));
  // New service tickets identify their own store. A stale plugin default must
  // never redirect credentials to a different service instance.
  const handoff = ticket.handoff;
  if (handoff && (handoff.version !== 1 || typeof handoff.data !== "string" || !path.isAbsolute(handoff.data))) {
    throw new Error("登录任务交接目录无效，请重新发起");
  }
  const data = handoff ? handoff.data : runtime.data;
  try {
    const saved = JSON.parse(await readFile(path.join(data, "runtime", "browser-login", ticket.ticket_id + ".json"), "utf8"));
    if (["ticket_id", "status", "expected_uid", "expires_at"].some(key => saved[key] !== ticket[key])
        || (handoff && saved.project_id !== handoff.project_id)) throw new Error();
  } catch {
    throw new Error("登录任务与交接目录不一致，请从当前管理服务重新发起；尚未读取凭证");
  }
  const cdp = await tab.capabilities.get("cdp");
  const cookies = selectedCookies(await cdp.send("Network.getCookies", { urls: [origin + "/"] }));
  const refreshed = await cdp.send("Runtime.evaluate", {
    expression: "localStorage.getItem('ac_time_value')", returnByValue: true,
  });
  const after = selectedCookies(await cdp.send("Network.getCookies", { urls: [origin + "/"] }));
  checkOrigin(await tab.url());
  if (Object.values(fields).some(field => cookies[field] !== after[field])) {
    throw new Error("读取期间登录态发生变化，请重新读取同一账号的页面");
  }
  const values = { ...cookies };
  if (typeof refreshed.result?.value === "string" && refreshed.result.value) {
    values.ac_time_value = refreshed.result.value;
  }
  if (values.dedeuserid !== ticket.expected_uid) {
    throw new Error("当前网页登录的不是项目绑定账号；原凭证未变");
  }
  for (const field of ["sessdata", "bili_jct", "dedeuserid", "buvid3", "buvid4"]) {
    if (!values[field]) throw new Error("网页登录凭证缺少必要字段，请完成登录后重试");
  }
  return await new Promise((resolve, reject) => {
    const child = spawn(runtime.python, [
      "-m", "bili_assistant.browser_login_transport", "--data", data, "--ticket", ticket.ticket_id,
    ], { cwd: runtime.project, windowsHide: true, stdio: ["pipe", "pipe", "pipe"], timeout: 20000 });
    let output = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      if (output.length + chunk.length > 16384) child.kill();
      else output += chunk;
    });
    child.stderr.on("data", () => {}); // Never echo raw child diagnostics.
    child.stdin.on("error", () => {});
    child.on("error", () => reject(new Error("本地凭证交接脚本无法运行，请检查插件安装")));
    child.on("close", code => {
      try {
        const result = JSON.parse(output);
        if (code !== 0 || result.status !== "staged" || result.ticket_id !== ticket.ticket_id) {
          reject(new Error("凭证暂存未完成，请检查登录任务状态或重新发起"));
        } else {
          resolve({ ticket_id: result.ticket_id, status: "staged", encrypted: true });
        }
      } catch {
        reject(new Error("本地凭证交接未返回有效结果；未输出敏感诊断"));
      }
    });
    child.stdin.end(JSON.stringify({ origin, credentials: values }));
  });
}
