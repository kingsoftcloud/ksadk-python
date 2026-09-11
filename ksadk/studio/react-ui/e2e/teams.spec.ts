import { expect, test, type Page } from "@playwright/test";
import {
  teamSnapshot,
  memberRef,
  teamExecution,
} from "../src/test/teamsFixtures";
import {
  TEAMS_API_VERSION,
  type GroupSnapshot,
} from "@kingsoftcloud/ksadk-web/teams";

/** Synthetic server responses, confined to tests. The production App and plugin mount are used unchanged. */
async function mockStudio(page: Page, snapshot = teamSnapshot()) {
  const writes: { path: string; body: any; csrf: string | undefined }[] = [];
  await page.addInitScript(() => {
    const contribution = {
      id: "teams",
      label: "团队",
      pluginId: "test-plugin-teams",
      order: 20,
    };
    let enabled = true;
    const listeners = new Set<() => void>();
    (window as any).__STUDIO_DSH__ = {
      sections: () => [],
      subscribe: () => () => {},
      attach: () => () => {},
      workspacePages: () => (enabled ? [contribution] : []),
      subscribeWorkspace: (listener: () => void) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      attachWorkspace: (
        _id: string,
        container: HTMLElement,
        props: Record<string, unknown>,
      ) => {
        const wrapper = document.createElement("div");
        wrapper.style.height = "100%";
        wrapper.style.minHeight = "0";
        container.append(wrapper);
        let disposed = false;
        let dispose: undefined | (() => void);
        (window as any).__STUDIO_APP__
          .mountWorkspace("teams", wrapper, props)
          .then((cleanup: () => void) => {
            if (disposed) cleanup();
            else dispose = cleanup;
          });
        return () => {
          disposed = true;
          dispose?.();
          wrapper.remove();
        };
      },
    };
    (window as any).disposeTeamsTestPlugin = () => {
      enabled = false;
      listeners.forEach((listener) => listener());
      window.dispatchEvent(new Event("studio:workspace-changed"));
    };
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const response = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (request.method() !== "GET")
      writes.push({
        path,
        body: request.postDataJSON(),
        csrf: request.headers()["x-csrf-token"],
      });
    if (path.endsWith("/system/bootstrap"))
      return response({ csrfToken: "test-csrf" });
    if (path.endsWith("/plugins/teams/lifecycle"))
      return response({
        enabled: true,
        apiVersion: TEAMS_API_VERSION,
        health: "ready",
        authorityRef: snapshot.group.authorityRef,
      });
    if (path.endsWith("/groups/bindings"))
      return response({
        items: snapshot.members.map((member) => ({
          ...member.binding,
          name: member.name,
        })),
      });
    if (path === "/api/v1/groups" && request.method() === "POST") {
      const input = request.postDataJSON();
      return response(
        { ...snapshot, group: { ...snapshot.group, name: input.name } },
        201,
      );
    }
    if (path === "/api/v1/groups")
      return response({
        items: [
          {
            ...snapshot.group,
            memberCount: 2,
            pendingCount: snapshot.interactions.length,
            unreadCount: 1,
            lastMessage: "两项结果验收后，我会整理可执行的改造方案。",
          },
        ],
      });
    if (path.endsWith("/execution")) return response(teamExecution(snapshot));
    if (path.endsWith("/conversation"))
      return response({ ref: memberRef(), items: [], cursor: 0 });
    if (path.endsWith("/events"))
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: ": subscribed\n\nevent: heartbeat\ndata: {}\n\n",
      });
    if (path.startsWith("/api/v1/groups/") && request.method() !== "GET")
      return response({
        status: "accepted",
        groupId: snapshot.group.groupId,
        messageId: "message-receipt",
        teamRunId: "team-run",
      });
    if (path === `/api/v1/groups/${snapshot.group.groupId}`)
      return response(snapshot);
    if (path === "/api/v1/agents") return response({ items: [] });
    if (path.includes("/workspace"))
      return response({ name: "协作工作区", path: "/test/teams-workspace" });
    return response({ items: [], ready: true });
  });
  return writes;
}

test("actual Studio shell mounts a contributed chat-first workspace; draft and focus survive graph drilldown", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockStudio(page);
  await page.goto("/#/workspace/teams?groupId=fixture-group");
  await expect(
    page.getByRole("button", { name: "团队", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(
    page.getByRole("heading", { name: "接口改造协作组" }),
  ).toBeVisible();
  await expect(page.locator(".team-graph-node")).toHaveCount(0);
  const layout = await page.locator(".team-workspace").evaluate(element => ({ top: element.getBoundingClientRect().top, bottom: element.getBoundingClientRect().bottom, viewport: window.innerHeight }));
  expect(layout.bottom).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.bottom).toBeGreaterThanOrEqual(layout.viewport - 1);
  expect(await page.getByRole("textbox", { name: "搜索团队" }).evaluate(element => getComputedStyle(element).borderTopWidth)).toBe("0px");
  await expect(
    page.getByRole("button", { name: "编排", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("textbox", { name: "给团队的消息" })
    .fill("草稿保留到返回群聊");
  const progress = page.getByRole("button", { name: /查看协作：/ });
  await progress.click();
  await page.getByRole("button", { name: /展开执行视图/ }).click();
  await expect(page.locator(".team-graph-node")).toHaveCount(3);
  await page.getByRole("button", { name: "返回聊天", exact: true }).click();
  await page.getByRole("button", { name: "关闭详情" }).click();
  await expect(page.getByRole("textbox", { name: "给团队的消息" })).toHaveValue(
    "草稿保留到返回群聊",
  );
  await expect(progress).toBeFocused();
  expect(
    await page
      .locator(".studio-teams-browser")
      .evaluate((element) =>
        Math.abs(element.getBoundingClientRect().bottom - innerHeight),
      ),
  ).toBeLessThan(2);
  await page.screenshot({
    path: "output/studio-teams-chat-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("creation uses server binding catalog, then directed message preserves CSRF and scoped identity", async ({
  page,
}) => {
  const writes = await mockStudio(page);
  await page.goto("/#/workspace/teams");
  await page
    .getByRole("complementary", { name: "团队列表" })
    .getByRole("button", { name: "创建团队", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "创建团队" });
  await dialog.getByRole("checkbox", { name: /协调助手/ }).check();
  await dialog.getByRole("checkbox", { name: /工程师/ }).check();
  await dialog.getByLabel("群组名称").fill("兼容性评审团队");
  await dialog.getByRole("button", { name: "创建团队", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const create = writes.find((write) => write.path === "/api/v1/groups")!;
  expect(create.csrf).toBe("test-csrf");
  expect(create.body.members.map((member: any) => member.bindingRef)).toEqual([
    "binding-leader",
    "binding-engineer",
  ]);
  await page
    .getByRole("combobox", { name: "接收成员" })
    .selectOption("engineer");
  await page
    .getByRole("textbox", { name: "给团队的消息" })
    .fill("检查异常路径");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect
    .poll(
      () => writes.filter((write) => write.path.endsWith("/messages")).length,
    )
    .toBe(1);
  expect(
    writes.find((write) => write.path.endsWith("/messages")),
  ).toMatchObject({
    csrf: "test-csrf",
    body: { intent: "directed", mentions: ["engineer"] },
  });
  expect(
    writes.some(
      (write) =>
        write.path.endsWith("/team-runs") ||
        write.path.includes("agent-control"),
    ),
  ).toBe(false);
});

test("same-named approval targets the correct member and plugin disposal removes live UI", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  const snapshot = teamSnapshot();
  snapshot.interactions = ["leader", "engineer"].map((member) => ({
    ref: { ...memberRef(member), interactionId: "same-name" },
    revision: 2,
    title: "确认执行",
    message: "需要你的确认",
    kind: "approval",
    status: "pending",
    createdAt: snapshot.group.createdAt,
  }));
  const writes = await mockStudio(page, snapshot);
  await page.goto("/#/workspace/teams?groupId=fixture-group");
  await page.getByRole("button", { name: /项需要处理/ }).click();
  await page
    .locator(".team-interaction-card")
    .filter({ hasText: "工程师" })
    .getByRole("button", { name: "同意", exact: true })
    .click();
  await expect(page.getByText("已提交，等待执行端确认")).toBeVisible();
  expect(
    writes.find((write) => write.path.endsWith("/interactions"))?.body.ref,
  ).toEqual({ ...memberRef(), interactionId: "same-name" });
  await page.screenshot({
    path: "output/studio-teams-approval-desktop.png",
    fullPage: true,
  });
  await page.evaluate(() => (window as any).disposeTeamsTestPlugin());
  await expect(page.locator(".team-workspace")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "团队", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "打开团队", exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

for (const width of [1024, 768, 390])
  test(`Studio ${width}px: details replace narrow chat and page never overflows`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockStudio(page);
    await page.goto("/#/workspace/teams?groupId=fixture-group");
    await page.getByRole("button", { name: /查看协作：/ }).click();
    await expect(
      page.getByRole("complementary", { name: "本轮协作" }),
    ).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "给团队的消息" }),
    ).not.toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `output/studio-teams-details-${width}.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "关闭详情" }).click();
    await expect(
      page.getByRole("textbox", { name: "给团队的消息" }),
    ).toBeVisible();
  });

test("execution API is requested only on expansion and real child invocation opens an existing member observer", async ({
  page,
}) => {
  const reads: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "GET") reads.push(request.url());
  });
  await mockStudio(page);
  await page.goto("/#/workspace/teams?groupId=fixture-group");
  await expect(
    page.getByRole("heading", { name: "接口改造协作组" }),
  ).toBeVisible();
  expect(reads.some((url) => url.includes("/execution?"))).toBe(false);
  await page.getByRole("button", { name: /查看协作：/ }).click();
  await page.getByRole("button", { name: /展开执行视图/ }).click();
  await page.getByRole("button", { name: "执行调用", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "真实执行调用链" }),
  ).toBeVisible();
  await page.getByRole("button", { name: /子执行.*兼容性子检查/ }).click();
  await expect(
    page.getByText("执行详情只读。关闭面板不会停止成员。"),
  ).toBeVisible();
  await expect
    .poll(() =>
      reads.some((url) => url.includes("/members/engineer/conversation?")),
    )
    .toBe(true);
  await page.getByRole("button", { name: "在群内 @成员" }).click();
  await expect(page.getByRole("combobox", { name: "接收成员" })).toHaveValue(
    "engineer",
  );
});

test("performance reference: 8 members, 200 nodes, send feedback and accepted event to DOM", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const snapshot = teamSnapshot();
  snapshot.members = Array.from({ length: 8 }, (_, index) => ({ ...snapshot.members[index % 2], memberId: `perf-member-${index}`, name: `参考成员 ${index + 1}`, role: index ? "member" : "leader" }));
  snapshot.group.leaderMemberId = snapshot.members[0].memberId;
  snapshot.tasks = Array.from({ length: 200 }, (_, index) => ({ ...snapshot.tasks[0], taskId: `perf-task-${index}`, title: `参考任务 ${index + 1}`, dependencies: [], attempts: [], ownerMemberId: snapshot.members[index % 8].memberId }));
  await mockStudio(page, snapshot);
  await page.addInitScript(() => {
    const perf = { eventStarts: {} as Record<string, number>, eventDom: [] as number[], sendStart: 0, sendFeedback: [] as number[] };
    (window as any).teamsPerformance = perf;
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const response = await realFetch(input, init);
      if (!String(input).includes('/events?') || !response.body) return response;
      return new Response(response.body.pipeThrough(new TransformStream({ transform(chunk, controller) {
        const text = new TextDecoder().decode(chunk);
        for (const marker of text.match(/perf-observed-\d+/g) || []) perf.eventStarts[marker] = performance.now();
        controller.enqueue(chunk);
      } })), { status: response.status, headers: response.headers });
    };
    document.addEventListener('click', event => {
      const button = (event.target as HTMLElement).closest('button');
      if (button?.textContent === '发送') perf.sendStart = performance.now();
    }, true);
    new MutationObserver(() => {
      const text = document.body?.textContent || '';
      for (const [marker, start] of Object.entries(perf.eventStarts)) if (text.includes(marker)) { perf.eventDom.push(performance.now() - start); delete perf.eventStarts[marker]; }
      if (perf.sendStart && [...document.querySelectorAll('button')].some(button => button.textContent === '正在发送' || button.textContent === '发送中…' || button.textContent === '发送中')) { perf.sendFeedback.push(performance.now() - perf.sendStart); perf.sendStart = 0; }
    }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  });
  let seq = 0;
  await page.route('**/api/v1/groups/fixture-group/events?**', async route => {
    const index = ++seq;
    if (index > 20) return route.fulfill({ contentType: 'text/event-stream', body: ': heartbeat\n\n' });
    const event = { apiVersion: TEAMS_API_VERSION, eventId: `perf-${index}`, groupId: snapshot.group.groupId, groupSeq: index, type: 'message.created', createdAt: snapshot.group.createdAt, payload: { message: { ...snapshot.messages[0], messageId: `perf-${index}`, sourceRefs: [], parts: [{ kind: 'text', text: `perf-observed-${index}` }] } } };
    await route.fulfill({ contentType: 'text/event-stream', body: `event: message\ndata: ${JSON.stringify(event)}\n\n` });
  });
  await page.route('**/api/v1/groups/fixture-group/messages', async route => {
    await new Promise(resolve => setTimeout(resolve, 60));
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ status: 'accepted', groupId: snapshot.group.groupId, messageId: 'receipt' }) });
  });
  await page.goto('/#/workspace/teams?groupId=fixture-group');
  await expect(page.getByRole('heading', { name: snapshot.group.name })).toBeVisible();
  for (let index=0; index<20; index++) {
    await page.getByRole('textbox', { name: '给团队的消息' }).fill(`反馈采样 ${index}`);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await expect(page.getByRole('textbox', { name: '给团队的消息' })).toHaveValue('');
  }
  await expect.poll(() => page.evaluate(() => (window as any).teamsPerformance.eventDom.length), { timeout: 30000 }).toBe(20);
  await page.getByRole('button', { name: /查看协作：/ }).click();
  const graphStart = await page.evaluate(() => performance.now());
  await page.getByRole('button', { name: /展开执行视图/ }).click();
  await expect(page.locator('.team-graph-node')).toHaveCount(200);
  const graphMs = await page.evaluate(start => performance.now() - start, graphStart);
  const measured = await page.evaluate(() => (window as any).teamsPerformance);
  expect(measured.sendFeedback).toHaveLength(20);
  const p95 = (values: number[]) => [...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];
  const report = { transport: 'Mock HTTP and finite SSE; production Studio + shared Web components; Chromium headless on local Vite', members: 8, nodes: 200, sendSamples: measured.sendFeedback.length, eventSamples: measured.eventDom.length, sendFeedbackP95Ms: p95(measured.sendFeedback), eventToDomP95Ms: p95(measured.eventDom), graph200NodesElapsedMs: graphMs, userAgent: await page.evaluate(() => navigator.userAgent), raw: measured };
  console.log('TEAMS_BROWSER_PERFORMANCE', JSON.stringify(report));
  await testInfo.attach('teams-browser-performance.json', { body: JSON.stringify(report, null, 2), contentType: 'application/json' });
});


test("structured file approvals show scoped paths and inert collapsible file previews", async ({ page }) => {
  const snapshot = teamSnapshot();
  snapshot.interactions = [{ ref: { ...memberRef(), interactionId: "file-approval" }, revision: 1, kind: "approval", status: "pending", title: "write_workspace_file", message: JSON.stringify({ arguments: { path: "review.md", content: "# Review\n<script>window.shouldNotRun = true</script>" }, risk: "high" }), createdAt: snapshot.group.createdAt }];
  await mockStudio(page, snapshot);
  await page.goto('/#/workspace/teams?groupId=fixture-group');
  await page.getByRole('button', { name: /项需要处理/ }).click();
  const card = page.locator('.team-interaction-card');
  await expect(card.getByText('文件位置', { exact: true })).toBeVisible();
  await expect(card.getByText('review.md', { exact: true })).toBeVisible();
  await expect(card.locator('pre')).not.toBeVisible();
  await card.getByText('查看待写入的文件内容', { exact: true }).click();
  await expect(card.locator('pre')).toContainText('<script>window.shouldNotRun = true</script>');
  expect(await page.evaluate(() => (window as any).shouldNotRun)).toBeUndefined();
  await page.screenshot({ path: 'output/studio-teams-readable-approval.png', fullPage: true });
});

for (const width of [1440, 768, 390]) {
  for (const theme of ['light', 'dark']) {
    test(`forms ${width}px ${theme}: composer and settings keep separate control geometry`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.addInitScript(theme => localStorage.setItem('agentkit-studio-theme', theme), theme);
      await mockStudio(page);
      await page.goto('/#/workspace/teams?groupId=fixture-group');
      const composer = page.getByRole('textbox', { name: '给团队的消息' });
      await composer.fill('第一行输入\n第二行输入');
      await expect(composer).toHaveCSS('border-top-width', '0px');
      await expect(composer).toHaveCSS('outline-style', 'none');
      const toolbar = page.locator('.team-composer-toolbar');
      if (width === 1440) {
        const recipient = await toolbar.getByRole('combobox', { name: '接收成员' }).boundingBox();
        const note = await toolbar.getByText('仅留言', { exact: true }).boundingBox();
        expect(Math.abs(recipient!.y - note!.y)).toBeLessThan(16);
      }
      const send = toolbar.getByRole('button', { name: '发送', exact: true });
      const controls = toolbar.locator(':scope > div');
      expect((await controls.boundingBox())!.x + (await controls.boundingBox())!.width).toBeLessThanOrEqual((await send.boundingBox())!.x);
      await page.locator('.team-header').getByRole('button', { name: '设置', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '团队设置' });
      await expect(dialog).toBeVisible();
      const checkbox = dialog.getByRole('checkbox', { name: /允许成员互相唤醒/ });
      await expect(checkbox).toHaveCSS('width', '16px');
      await expect(checkbox).toHaveCSS('height', '16px');
      const bounds = await dialog.boundingBox();
      expect(bounds!.height).toBeLessThan(900);
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
      const fields = await dialog.locator('.team-field').evaluateAll(elements => elements.map(e => {
        const r = e.getBoundingClientRect();
        const control = e.querySelector('input,select')!.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, controlTop: control.top, controlBottom: control.bottom };
      }));
      for (let i = 0; i < fields.length; i++) {
        expect(fields[i].controlTop).toBeGreaterThanOrEqual(fields[i].top);
        expect(fields[i].controlBottom).toBeLessThanOrEqual(fields[i].bottom + 1);
        if (i) expect(fields[i].top).toBeGreaterThanOrEqual(fields[i - 1].bottom);
      }
      await expect(dialog).toHaveCSS('background-color', theme === 'dark' ? 'rgb(32, 33, 35)' : 'rgb(255, 255, 255)');
      await page.screenshot({ path: `output/ui-review/settings-${width}-${theme}.png` });
      await dialog.getByRole('button', { name: '取消', exact: true }).click();
      await expect(composer).toHaveValue('第一行输入\n第二行输入');
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: `output/ui-review/team-${width}-${theme}.png` });
    });
  }
}

for (const width of [1440, 390]) {
  test(`shared forms ${width}px: search icons, resource tabs and consecutive settings rows`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await mockStudio(page);
    await page.goto('/#/resources/model');
    const search = page.getByPlaceholder('搜索资源');
    await search.fill('模型');
    const icon = await page.locator('.search-field > svg').boundingBox();
    const input = await search.boundingBox();
    expect(input!.x).toBeGreaterThanOrEqual(icon!.x + icon!.width + 6);
    await expect(search).toHaveCSS('border-left-width', '0px');
    await expect(search).toHaveCSS('outline-style', 'none');
    const tabs = page.getByRole('tablist', { name: '资源类型' });
    for (const tab of await tabs.getByRole('tab').all()) {
      await expect(tab).toHaveCSS('white-space', 'nowrap');
      expect((await tab.boundingBox())!.height).toBeLessThan(55);
    }
    await page.getByRole('button', { name: '配置模型', exact: true }).click();
    const probe = page.getByRole('button', { name: '智能探测', exact: true });
    const probeRect = await probe.boundingBox();
    expect(probeRect!.x + probeRect!.width).toBeLessThan(width - 16);
    await expect(page.getByRole('button', { name: '完整 endpointUrl', exact: true })).toHaveCSS('white-space', 'nowrap');
    const modes = await page.locator('.model-endpoint-actions .segmented-control').evaluate(e => ({ columns: getComputedStyle(e).gridTemplateColumns.split(' ').length, count: e.children.length, clipped: [...e.children].some(c => c.scrollWidth > c.clientWidth + 1) }));
    expect(modes.columns).toBe(modes.count);
    expect(modes.clipped).toBe(false);
    await page.screenshot({ animations: "disabled", path: `output/ui-review/model-form-regression-${width}.png` });
    await page.getByRole('button', { name: '取消', exact: true }).click();
    if (width === 390) await page.getByRole('button', { name: '展开导航', exact: true }).click();
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByRole('button', { name: '云端连接', exact: true }).click();
    const firstRow = page.locator('#settings-cloud .form-grid').first();
    const nextRow = page.locator('#settings-cloud .form-grid').nth(1);
    const first = await firstRow.boundingBox(); const next = await nextRow.boundingBox();
    expect(next!.y - (first!.y + first!.height)).toBeGreaterThanOrEqual(20);
    await page.screenshot({ path: `output/ui-review/cloud-form-${width}.png` });
  });
}
