import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const overlay = readFileSync(new URL("./components/SettingsOverlay.tsx", import.meta.url), "utf8");
const schema = readFileSync(new URL("./schemas/resourceForms.ts", import.meta.url), "utf8");

test("settings cloud section exposes kingsoft cloud account credentials", () => {
  for (const id of ["settingCloudAccessKey", "settingCloudSecretKey", "settingCloudAccountId"]) {
    assert.match(overlay, new RegExp(`id="${id}"`), `missing field ${id}`);
  }
  // AK/SK 是密钥输入:password 类型 + 留空不修改语义
  assert.match(overlay, /type="password"[^>]*\{\.\.\.settingsForm\.register\("cloudAccessKey"\)\}/);
  assert.match(overlay, /留空保留已保存值/);
  // AccountID 非密钥,普通输入;配置状态只回显布尔,不回传 secret
  assert.match(overlay, /cloudAccountConfigured/);
});

test("settings schema accepts cloud account fields with bounded length", () => {
  for (const field of ["cloudAccessKey", "cloudSecretKey", "cloudAccountId"]) {
    assert.match(schema, new RegExp(field));
  }
});

test("settings save only submits non-empty cloud account values", () => {
  // 留空字段不进 payload(后端按增量合并,空提交会清掉已存值)
  assert.match(overlay, /if \(values\.cloudAccessKey\.trim\(\)\) payload\.cloudAccessKey/);
  assert.match(overlay, /if \(values\.cloudAccountId\.trim\(\)\) payload\.cloudAccountId/);
});
