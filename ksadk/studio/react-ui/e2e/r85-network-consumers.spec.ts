import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

// Input is emitted by test_local_managed_network.py, never hand-authored child facts.
const input = process.env.R85_CONVERSATION_ITEMS;
test.skip(!input, 'Run the real local managed network gate and supply its conversation-items.json');
const frames = input ? JSON.parse(readFileSync(input, 'utf8')) : [];
const canonical = input ? JSON.parse(readFileSync(join(dirname(input), 'full-canonical.json'), 'utf8')) : [];
for (const [consumer, url] of Object.entries({
  embedded: process.env.R85_EMBEDDED_URL,
  hosted: process.env.R85_HOSTED_URL,
  studio: process.env.R85_STUDIO_URL,
})) {
  test(`${consumer} renders real native A2A output`, async ({ page, request }) => {
    test.skip(!url, `Supply R85_${consumer.toUpperCase()}_URL`);
    const errors: string[] = [];
    let sent = false;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const path = new URL(route.request().url()).pathname;
      const json = (data: unknown) => route.fulfill({json: data});
      if (path.includes('/conversation-surface')) return json({buildId:'r85-build', surface:{
        apiVersion:'conversation.ksadk.io/v1', kind:'ConversationSurface',surfaceId:'r85',sessionId:'s',providerRef:'agent.provider/v1:r85',
        inputs:[{name:'text',mode:'native'},{name:'ksadk.presentation',mode:'native'}],
        outputs:[{name:'text',mode:'native'},{name:'tool.inspect',mode:'native'},{name:'agent.block',mode:'native'}],
      }});
      if (path.endsWith('/conversation:stream') || path.endsWith('/events')) {
        sent = true;
        if (process.env.R85_STAGED_STREAM_URL && path.endsWith('/conversation:stream')) return route.continue({url:process.env.R85_STAGED_STREAM_URL + '/stream'});
        return route.fulfill({contentType:'text/event-stream',body: frames.map((f: any) => `id: ${f.seq}\ndata: ${JSON.stringify({conversationItem:f.conversationItem})}\n\n`).join('')});
      }
      if (path.includes('/agentengine/api/v1/')) {
        const action = path.split('/').pop()!;
        const payloads: Record<string, unknown> = {
          GetAgentUiBootstrap:{Agent:{AgentId:'r85-agent',Name:'R85 Network',Framework:'adk'},ApiFormats:['responses'],Capabilities:{StopRun:true,WorkspaceFiles:false,RunLifecycle:{Enabled:false}},HostedChat:{PreferredTransport:'responses',Transports:[{Protocol:'responses',Runtime:'adk',Endpoint:'/v1/responses',Version:'v1'}]}},
          ListSessions:{Sessions:[{SessionId:'s',AgentId:'r85-agent',Title:'R85 network',UpdatedAt:'2026-09-14T00:00:00Z'}],Total:1},
          GetSession:{Session:{SessionId:'s',AgentId:'r85-agent',ActiveRunStatus:''}},
          CreateSession:{SessionId:'s',Session:{SessionId:'s',AgentId:'r85-agent'}},
          ListAgentModels:{Models:[],Current:'',Source:'fixture'},
          ListSessionMessages:{Messages:[],LatestSeqId:0,HasMore:false,NextCursor:null},
          ListSessionEvents:{Events:sent ? canonical.map((event:any, i:number) => ({SeqId:i+1,Timestamp:event.timestamp,Content:{runtime_event:event}})) : [],Total:sent ? canonical.length : 0},ListSessionCheckpoints:{Checkpoints:[]},ListToolReceipts:{ToolReceipts:[]},GetResponseFeedback:null,
        };
        return json({Code:0,Message:'Success',Data:payloads[action] ?? {}});
      }
      return route.continue();
    });
    await page.goto(url!);
    const composer = page.locator('textarea').first();
    await expect(composer).toBeVisible();
    if (consumer === 'studio') await expect(page.getByRole('heading', {name:'R85 network',exact:true})).toBeVisible();
    await composer.fill('Run the locally verified remote expert');
    await composer.press('Enter');
    if (process.env.R85_STAGED_STREAM_URL) {
      const liveBlock = page.getByTestId('agent-block');
      await expect(liveBlock).toHaveCount(1);
      await liveBlock.getByRole('button', {name:'expert',exact:true}).click();
      await expect(liveBlock.getByTestId('agent-block-detail').getByText('first answer', {exact:true})).toBeVisible();
      await expect(liveBlock.getByRole('button', {name:/正在运行 weather/})).toBeVisible();
      await expect(page.getByText('root continued', {exact:true})).toHaveCount(0);
      await page.screenshot({path:test.info().outputPath(`${consumer}-tool-running.png`),fullPage:true});
      await request.get(process.env.R85_STAGED_STREAM_URL + '/advance');
      await expect(liveBlock.getByRole('status')).toHaveText('已完成');
      await expect(page.getByRole('button', {name:'保留恢复点并结束本次执行'})).toBeVisible();
      await expect(page.getByText('root continued', {exact:true})).toHaveCount(0);
      await page.screenshot({path:test.info().outputPath(`${consumer}-child-completed.png`),fullPage:true});
      await request.get(process.env.R85_STAGED_STREAM_URL + '/advance');
    }
    await expect(page.getByText('root continued', {exact:true})).toBeVisible();
    const block = page.getByTestId('agent-block');
    await expect(block).toHaveCount(1);
    const toggle = block.getByRole('button', {name:'expert',exact:true});
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
    const detail = block.getByTestId('agent-block-detail');
    await expect(detail.getByText('first answer', {exact:true})).toHaveCount(1);
    await expect(detail.getByText('second answer', {exact:true})).toHaveCount(1);
    await expect(page.getByText('fixture-private-value', {exact:false})).toHaveCount(0);
    await page.reload();
    await expect(page.getByText('root continued', {exact:true})).toBeVisible();
    await expect(block).toHaveCount(1);
    const expand = block.getByRole('button', {name:'expert',exact:true});
    if (await expand.getAttribute('aria-expanded') !== 'true') await expand.click();
    await expect(detail.getByText('first answer', {exact:true})).toHaveCount(1);
    await expect(detail.getByText('second answer', {exact:true})).toHaveCount(1);
    expect(errors).toEqual([]);
    await page.screenshot({path:test.info().outputPath(`${consumer}.png`),fullPage:true});
  });
}
