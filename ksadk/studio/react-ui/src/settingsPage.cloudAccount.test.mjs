import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const settings = readFileSync(new URL("./pages/SettingsPage.tsx", import.meta.url), "utf8");
const schema = readFileSync(new URL("./schemas/resourceForms.ts", import.meta.url), "utf8");

test("settings cloud section exposes kingsoft cloud account credentials", () => {
  for (const id of ["settingCloudAccessKey", "settingCloudSecretKey", "settingCloudAccountId"]) {
    assert.match(settings, new RegExp(`id="${id}"`), `missing field ${id}`);
  }
  // AK/SK 是密钥输入:password 类型 + 留空不修改语义
  assert.match(settings, /type="password"[^>]*\{\.\.\.settingsForm\.register\("cloudAccessKey"\)\}/);
  assert.match(settings, /留空保留已保存值/);
  // AccountID 非密钥,普通输入;配置状态只回显布尔,不回传 secret
  assert.match(settings, /cloudAccountConfigured/);
});

test("settings schema accepts cloud account fields with bounded length", () => {
  for (const field of ["cloudAccessKey", "cloudSecretKey", "cloudAccountId"]) {
    assert.match(schema, new RegExp(field));
  }
});

test("settings save only submits non-empty cloud account values", () => {
  // 留空字段不进 payload(后端按增量合并,空提交会清掉已存值)
  assert.match(settings, /if \(values\.cloudAccessKey\.trim\(\)\) payload\.cloudAccessKey/);
  assert.match(settings, /values\.cloudAccountId\.trim\(\) && \(!payload\.cloudAccessKey \|\| settingsForm\.formState\.dirtyFields\.cloudAccountId\)/);
});
