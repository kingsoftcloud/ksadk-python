import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, MessageSquare, Plus, Send, Users } from "lucide-react";
import { showToast } from "../components/Toast";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Drawer } from "../components/Drawer";
import { FormField } from "../components/ui/FormField";
import { StudioDataTable, type StudioDataColumn } from "../components/ui/StudioDataTable";
import { StudioSelect } from "../components/ui/StudioSelect";
import {
  CHANNEL_LABELS,
  channelApi,
  DM_POLICY_OPTIONS,
  DM_POLICY_LABELS,
  formatChannelDate,
  GROUP_POLICY_OPTIONS,
  GROUP_POLICY_LABELS,
  PLATFORM_BADGE,
  SESSION_SCOPE_OPTIONS,
  type Channel,
  type ChannelType,
  type DmPolicy,
  type GroupPolicy,
  type ListResult,
  type SessionScope,
} from "./channelApi";
import "./channels.css";

// 平台选择卡片配置
const PLATFORM_LIST: Array<{ value: ChannelType; label: string; desc: string; Icon: typeof MessageSquare }> = [
  { value: "wps-xiezuo", label: "WPS 协作", desc: "金山办公协作平台", Icon: MessageSquare },
  { value: "feishu", label: "飞书", desc: "字节跳动企业协作", Icon: Send },
  { value: "wecom", label: "企业微信", desc: "腾讯企业即时通讯", Icon: Users },
];

const EMPTY_FORM = {
  Channel: "wps-xiezuo" as ChannelType,
  ChannelAccountId: "",
  AppId: "",
  AppSecret: "",
  DmPolicy: "pairing" as DmPolicy,
  GroupPolicy: "allowlist" as GroupPolicy,
  RequireMention: true,
  SessionScope: "per-peer" as SessionScope,
  Enabled: true,
  ConfigJson: "{}",
};

export function AgentChannelTab({ agentId, agentName, refreshTick }: {
  agentId: string;
  agentName: string;
  refreshTick: number;
}) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Channel | null>(null);
  const [deleting, setDeleting] = useState(false);

  const loadChannels = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await channelApi<ListResult<Channel>>("ListChannels", { Offset: 0, Limit: 100 });
      setChannels((data.Items || []).filter(ch => ch.AgentId === agentId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "渠道加载失败，请稍后重试。");
      setChannels([]);
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => { void loadChannels(); }, [loadChannels, refreshTick]);

  const enabledCount = channels.filter(c => c.Enabled).length;
  const formValid = form.ChannelAccountId.trim() && form.AppId.trim() && form.AppSecret.trim();

  function openCreate() {
    setForm({ ...EMPTY_FORM });
    setAdvancedOpen(false);
    setFormOpen(true);
  }

  async function submitChannel(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      const body: Record<string, unknown> = {
        AgentId: agentId,
        Channel: form.Channel,
        ChannelAccountId: form.ChannelAccountId.trim(),
        AppId: form.AppId.trim(),
        AppSecret: form.AppSecret.trim(),
        DmPolicy: form.DmPolicy,
        GroupPolicy: form.GroupPolicy,
        RequireMention: form.RequireMention,
        SessionScope: form.SessionScope,
        Enabled: form.Enabled,
        ConfigJson: form.ConfigJson || "{}",
      };
      await channelApi("CreateChannel", body);
      showToast("渠道已创建", `${CHANNEL_LABELS[form.Channel]} · ${form.ChannelAccountId}`);
      setFormOpen(false);
      await loadChannels();
    } catch (e) {
      showToast("渠道创建失败", e instanceof Error ? e.message : "请稍后重试", "error");
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await channelApi("DeleteChannel", { Id: deleteTarget.Id });
      setChannels(prev => prev.filter(c => c.Id !== deleteTarget.Id));
      showToast("渠道已解绑", `${CHANNEL_LABELS[deleteTarget.Channel]} · ${deleteTarget.ChannelAccountId}`);
      setDeleteTarget(null);
    } catch (e) {
      showToast("解绑失败", e instanceof Error ? e.message : "请稍后重试", "error");
    } finally {
      setDeleting(false);
    }
  }

  const channelColumns = useMemo<StudioDataColumn<Channel>[]>(() => [
    {
      id: "name",
      header: "渠道",
      minWidth: 220,
      cell: ch => (
        <>
          <span className="channel-platform-tag" data-platform={ch.Channel} aria-hidden="true">{PLATFORM_BADGE[ch.Channel]}</span>
          <strong>{CHANNEL_LABELS[ch.Channel]} · {ch.ChannelAccountId}</strong>
          <span className="resource-origin mono">{ch.Id}</span>
        </>
      ),
    },
    {
      id: "enabled",
      header: "状态",
      width: 100,
      cell: ch => <span className="badge" data-state={ch.Enabled ? "ready" : "idle"}>{ch.Enabled ? "已启用" : "已停用"}</span>,
    },
    {
      id: "dmPolicy",
      header: "私聊策略",
      width: 100,
      cell: ch => <span className="channel-policy-tag" data-policy={ch.DmPolicy}>{DM_POLICY_LABELS[ch.DmPolicy]}</span>,
    },
    {
      id: "groupPolicy",
      header: "群聊策略",
      width: 100,
      cell: ch => <span className="channel-policy-tag" data-policy={ch.GroupPolicy}>{GROUP_POLICY_LABELS[ch.GroupPolicy]}</span>,
    },
    {
      id: "createdAt",
      header: "创建时间",
      minWidth: 140,
      cell: ch => formatChannelDate(ch.CreatedAt),
    },
    {
      id: "actions",
      header: "",
      width: 72,
      className: "channels-page__row-actions",
      cell: ch => (
        <button
          className="button tertiary small"
          type="button"
          onClick={() => setDeleteTarget(ch)}
        >
          解绑
        </button>
      ),
    },
  ], []);

  return (
    <div className="agent-channel-tab">
      <section className="agent-channel-tab__summary" aria-label="渠道汇总">
        <div><span>已绑定渠道</span><strong>{channels.length}</strong></div>
        <div><span>已启用</span><strong>{enabledCount}</strong></div>
        <div><span>当前 Agent</span><strong>{agentName}</strong></div>
      </section>

      <section className="channels-page__panel" aria-label="渠道列表">
        <div className="channels-page__panel-header">
          <div>
            <strong>IM 渠道</strong>
            <span>绑定 IM 平台后，用户即可在这些渠道直接对话该 Agent</span>
          </div>
          <button className="button accent" type="button" onClick={openCreate}>
            <Plus size={15} /><span>绑定新渠道</span>
          </button>
        </div>
        {error && <div className="form-error" style={{ margin: "0 16px 12px" }}>{error}</div>}
        <StudioDataTable
          columns={channelColumns}
          data={channels}
          getRowId={ch => ch.Id}
          caption="Agent IM 渠道列表"
          minWidth={760}
          loading={loading}
          empty={{ icon: <MessageSquare size={22} />, title: "尚未绑定 IM 渠道", description: "点击「绑定新渠道」为该 Agent 接入 IM 平台。" }}
        />
      </section>

      {formOpen && (
        <Drawer
          title="绑定新渠道"
          subtitle="为当前 Agent 配置 IM 平台接入信息与对话策略。"
          wide
          closeDisabled={submitting}
          onClose={() => setFormOpen(false)}
          footer={(
            <>
              <span className="drawer-footer-spacer" />
              <button className="button tertiary" type="button" onClick={() => setFormOpen(false)} disabled={submitting}>取消</button>
              <button className="button accent" type="submit" form="agent-channel-create-form" disabled={submitting || !formValid}>
                {submitting ? "正在绑定" : "绑定渠道"}
              </button>
            </>
          )}
        >
          <form id="agent-channel-create-form" className="channels-page__create-form" onSubmit={submitChannel}>
            {/* Step 1: 平台选择 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="1">选择平台</div>
              <div className="channels-page__platform-cards">
                {PLATFORM_LIST.map(({ value, label, desc, Icon }) => (
                  <button
                    key={value}
                    type="button"
                    className={`channels-page__platform-card${form.Channel === value ? " selected" : ""}`}
                    onClick={() => setForm(prev => ({ ...prev, Channel: value }))}
                  >
                    <span className="channels-page__platform-card-icon"><Icon size={22} /></span>
                    <span className="channels-page__platform-card-text">
                      <span className="channels-page__platform-card-name">{label}</span>
                      <span className="channels-page__platform-card-desc">{desc}</span>
                    </span>
                    {form.Channel === value && <Check size={14} className="channels-page__platform-card-check" />}
                  </button>
                ))}
              </div>
            </div>

            {/* Step 2: 接入配置 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="2">接入配置</div>
              <div className="channels-page__form-grid">
                <div className="channels-page__form-col-12">
                  <FormField label="渠道账号 ID" htmlFor="agent-channel-account-id" requirement="required">
                    <input
                      id="agent-channel-account-id"
                      value={form.ChannelAccountId}
                      onChange={event => setForm(prev => ({ ...prev, ChannelAccountId: event.target.value }))}
                      placeholder="如：wps-default"
                      required
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <FormField label="App ID" htmlFor="agent-channel-app-id" requirement="required">
                    <input
                      id="agent-channel-app-id"
                      value={form.AppId}
                      onChange={event => setForm(prev => ({ ...prev, AppId: event.target.value }))}
                      placeholder="平台分配的应用 ID"
                      required
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <FormField label="App Secret" htmlFor="agent-channel-app-secret" requirement="required">
                    <input
                      id="agent-channel-app-secret"
                      type="password"
                      value={form.AppSecret}
                      onChange={event => setForm(prev => ({ ...prev, AppSecret: event.target.value }))}
                      placeholder="平台分配的应用密钥"
                      required
                    />
                  </FormField>
                </div>
              </div>
            </div>

            {/* Step 3: 策略与启用 */}
            <div className="channels-page__form-section">
              <div className="channels-page__form-section-title" data-step="3">策略与启用</div>
              <div className="channels-page__form-grid">
                <div className="channels-page__form-col-6">
                  <FormField label="私聊策略">
                    <StudioSelect
                      ariaLabel="私聊策略"
                      value={form.DmPolicy}
                      options={DM_POLICY_OPTIONS}
                      onValueChange={value => setForm(prev => ({ ...prev, DmPolicy: value as DmPolicy }))}
                    />
                  </FormField>
                </div>
                <div className="channels-page__form-col-6">
                  <FormField label="群聊策略">
                    <StudioSelect
                      ariaLabel="群聊策略"
                      value={form.GroupPolicy}
                      options={GROUP_POLICY_OPTIONS}
                      onValueChange={value => setForm(prev => ({ ...prev, GroupPolicy: value as GroupPolicy }))}
                    />
                  </FormField>
                </div>
              </div>
            </div>

            {/* 高级设置 */}
            <div className="channels-page__advanced-toggle">
              <button
                type="button"
                className="channels-page__advanced-header"
                onClick={() => setAdvancedOpen(prev => !prev)}
                aria-expanded={advancedOpen}
              >
                <ChevronDown
                  size={16}
                  className={`channels-page__chevron${advancedOpen ? "" : " channels-page__chevron--closed"}`}
                />
                <span>高级设置</span>
                <small>会话隔离、@机器人、启用状态等</small>
              </button>
            </div>
            {advancedOpen && (
              <div className="channels-page__advanced-body">
                <div className="channels-page__advanced-section">
                  <div className="channels-page__advanced-section-title">会话设置</div>
                  <div className="channels-page__form-grid">
                    <div className="channels-page__form-col-6">
                      <FormField label="会话隔离">
                        <StudioSelect
                          ariaLabel="会话隔离"
                          value={form.SessionScope}
                          options={SESSION_SCOPE_OPTIONS}
                          onValueChange={value => setForm(prev => ({ ...prev, SessionScope: value as SessionScope }))}
                        />
                      </FormField>
                    </div>
                    <div className="channels-page__form-col-6">
                      <label className="channel-toggle-row channel-toggle-row--compact">
                        <input
                          type="checkbox"
                          checked={form.RequireMention}
                          onChange={event => setForm(prev => ({ ...prev, RequireMention: event.target.checked }))}
                        />
                        <span>
                          <strong>群聊 @机器人</strong>
                          <small>开启后群聊中需 @机器人才会触发响应</small>
                        </span>
                      </label>
                    </div>
                  </div>
                </div>
                <div className="channels-page__advanced-section">
                  <label className="channel-toggle-row">
                    <input
                      type="checkbox"
                      checked={form.Enabled}
                      onChange={event => setForm(prev => ({ ...prev, Enabled: event.target.checked }))}
                    />
                    <span>
                      <strong>启用渠道</strong>
                      <small>停用后渠道不再接收和发送消息</small>
                    </span>
                  </label>
                </div>
              </div>
            )}
          </form>
        </Drawer>
      )}

      {deleteTarget && (
        <ConfirmDialog
          title="解绑渠道"
          description={`确定要解绑「${CHANNEL_LABELS[deleteTarget.Channel]} · ${deleteTarget.ChannelAccountId}」吗？已绑定的会话将断开连接。`}
          confirmText="解绑"
          danger
          busy={deleting}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
