import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Card, Input, Button, Avatar, Tooltip, Typography, Spin, Empty, Tag, Popconfirm, message } from 'antd';
import {
  SendOutlined, PlusOutlined, RobotOutlined, ToolOutlined, DeleteOutlined, StopOutlined, WarningOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import ConfirmModal from './ConfirmModal';
import {
  startChatStream, confirmAction, listConversations, fetchConversationMessages, deleteConversation,
} from '../services/agent';
import { useAuth } from '../contexts/AuthContext';

const { Text } = Typography;

// 流式增量合并刷渲染的间隔：逐 token setState 是 O(消息数×token 数) 的重渲染
const STREAM_FLUSH_MS = 100;
// 同一时刻允许并行的生成流上限（后端无用户级锁，上限纯为保护 DashScope 并发配额）
const MAX_ACTIVE_STREAMS = 3;
// 非流式会话池的 LRU 保留上限：最近打开过的会话切回免重新拉取，超出的池回收
const MAX_IDLE_POOLS = 8;

const MsgList = styled.div`
  height: 520px;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
`;

const BubbleRow = styled.div`
  display: flex;
  gap: 8px;
  align-items: flex-start;
  ${(p) => p.$isUser && 'flex-direction: row-reverse;'}
`;

const AvatarWrap = styled.div`
  flex: none;
  padding-top: 2px;
`;

const Bubble = styled.div`
  max-width: 82%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  ${(p) => (p.$isUser
    ? 'background: #2563eb; color: #fff; border-bottom-right-radius: 4px;'
    : 'background: #fff; border: 1px solid #eef0f4; border-bottom-left-radius: 4px; box-shadow: 0 1px 2px rgba(15,23,42,0.04);')}
`;

const ToolCard = styled.div`
  background: #fff;
  border: 1px dashed #d9dde5;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 12.5px;
  color: #595959;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: 40px;
`;

const SystemLine = styled.div`
  text-align: center;
  font-size: 12.5px;
  color: #8c95a3;
`;

const ThinkingRow = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
  color: #8c95a3;
  font-size: 13px;
  margin-left: 40px;
`;

const InputBar = styled.div`
  padding: 12px 14px 10px;
  background: #fff;
  border-top: 1px solid #eef0f4;
`;

const Hint = styled.div`
  margin-top: 6px;
  font-size: 11px;
  color: #b3bac4;
  text-align: center;
`;

const SuggestBox = styled.div`
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
`;

const ChatLayout = styled.div`
  display: flex;
  @media (max-width: 768px) {
    flex-direction: column;
  }
`;

const ConvSidebar = styled.div`
  width: 216px;
  flex: none;
  border-right: 1px solid #eef0f4;
  overflow-y: auto;
  max-height: 620px;
  padding: 8px 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  @media (max-width: 768px) {
    width: 100%;
    max-height: 148px;
    border-right: none;
    border-bottom: 1px solid #eef0f4;
  }
`;

const ConvItem = styled.div`
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 6px 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
  &:hover {
    background: #f5f7fa;
  }
  &:hover .del-btn {
    opacity: 1;
  }
  ${(p) => p.$active && 'background: #eff6ff; border-color: #bfdbfe;'}
  .del-btn {
    opacity: 0;
    flex: none;
  }
`;

// 侧栏条目右侧的运行状态区：生成中 spinner / 待确认警告 / 停止与删除按钮
const ConvStatus = styled.div`
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
`;

const SUGGESTIONS = [
  '帮我找一下猫的照片',
  '最近上传了什么文件',
  '我的存储空间用了多少',
];

// ── 轻量富文本渲染：Markdown 链接/裸 URL 转可点击 <a>，**加粗** 转 <strong> ──
// 不引入 markdown 库：助手输出以短句+链接为主，文本节点经 React 转义无 XSS 面，
// 链接一律 target=_blank + rel=noopener 且仅放行 http(s)。
const MD_LINK_RE = /\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g;
// 排除中文字符与中日韩标点，避免"URL 后紧跟中文无空格"时把中文吞进链接
const BARE_URL_RE = /https?:\/\/[^\s<>()\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+/g;

function pushPlainText(text, parts) {
  text.split(/(\*\*[^*]+\*\*)/g).forEach((seg) => {
    if (!seg) return;
    if (seg.startsWith('**') && seg.endsWith('**')) {
      parts.push(<strong key={parts.length}>{seg.slice(2, -2)}</strong>);
      return;
    }
    let last = 0;
    let mm;
    BARE_URL_RE.lastIndex = 0;
    while ((mm = BARE_URL_RE.exec(seg)) !== null) {
      if (mm.index > last) parts.push(seg.slice(last, mm.index));
      parts.push(
        <a key={parts.length} href={mm[0]} target="_blank" rel="noopener noreferrer">
          {mm[0]}
        </a>
      );
      last = mm.index + mm[0].length;
    }
    if (last < seg.length) parts.push(seg.slice(last));
  });
}

function renderRich(content) {
  const parts = [];
  let last = 0;
  let m;
  MD_LINK_RE.lastIndex = 0;
  while ((m = MD_LINK_RE.exec(content)) !== null) {
    pushPlainText(content.slice(last, m.index), parts);
    parts.push(
      <a key={parts.length} href={m[2]} target="_blank" rel="noopener noreferrer">
        {m[1]}
      </a>
    );
    last = m.index + m[0].length;
  }
  pushPlainText(content.slice(last), parts);
  return parts;
}

/**
 * 服务端消息 → 聊天视图模型。
 * 库里的 assistant 消息可能只带 tool_calls（无正文），不直接渲染；
 * 工具名由其 tool_calls 的 id→name 映射回填到后续 tool 消息上。
 * 保留 seq 供"加载更早消息"翻页（before_seq 取首条消息的 seq）。
 */
const mapHistoryMessages = (msgs) => {
  const toolNameById = {};
  const out = [];
  msgs.forEach((m) => {
    if (m.role === 'user') {
      out.push({ role: 'user', content: m.content, seq: m.seq });
    } else if (m.role === 'assistant') {
      (m.tool_calls || []).forEach((tc) => {
        toolNameById[tc.id] = tc.function?.name;
      });
      if (m.content) out.push({ role: 'assistant', content: m.content, seq: m.seq });
    } else if (m.role === 'tool') {
      let result = null;
      try { result = JSON.parse(m.content); } catch { result = null; }
      out.push({
        role: 'tool',
        toolName: toolNameById[m.tool_call_id] || '工具调用',
        toolArgs: {},
        toolResult: result,
        seq: m.seq,
      });
    }
  });
  return out;
};

// ── 会话池 ──
// 每个会话一份状态 + 一份运行时：
//   池键 key：历史会话为服务端 conv_key；新对话先占本地临时键 tmp:N，
//   done 事件回传真实 conv_id 后迁移成真实键（侧栏/激活指针同步换键）。
//   ConvState（immutable，进 React state）：消息、流式标记、确认单、输入草稿……
//   runtime（可变，进 ref）：gen 代际计数器、AbortController、增量缓冲与节流计时器。
const makeConvState = (key) => ({
  key,
  messages: [],
  streaming: false,
  thinking: false,
  confirmData: null,
  hasOlder: false,
  loadingOlder: false,
  input: '',
  lastAccess: 0,       // LRU 淘汰依据
});

const makeRuntime = () => ({ gen: 0, abortCtrl: null, deltaBuf: '', flushTimer: null });

/**
 * AI 文件助手（首页主体，全站唯一的文件搜索入口）
 * 找文件直接在对话里描述内容即可；助手同时负责文件管理操作与危险操作确认。
 * 对话历史持久化在服务端（MySQL），左侧栏可查看/恢复/删除历史会话。
 *
 * 并发模型：会话状态池化。组件只持有 activeKey 指向当前展示的池，切换会话/
 * 新建对话是纯视图切换——不掐断在途流，流在后台继续写自己的池，切回即见实时
 * 进度；多个会话可并行生成（上限 MAX_ACTIVE_STREAMS）。隔离手段按池分键：
 *   gen 代际：作废"停止/删除/同会话重发"之后晚到的流回调，防止写已停的池；
 *   增量缓冲：非激活会话的高频 token 增量暂留缓冲不刷 state（零渲染开销），
 *   切回时一次性刷出；低频事件（工具调用/done）始终即时落池，侧栏状态实时。
 */
export default function AssistantChat() {
  const { user, logout } = useAuth();

  const [convStates, setConvStates] = useState({});   // key -> ConvState
  const [activeKey, setActiveKey] = useState(null);   // 当前展示的池键
  const convStatesRef = useRef({});
  convStatesRef.current = convStates;                 // 流回调内读最新池状态（免闭包过期）
  const activeKeyRef = useRef(null);
  const runtimesRef = useRef(new Map());              // key -> runtime
  const tmpSeqRef = useRef(0);                        // 本地临时键发号
  const openSeqRef = useRef(0);                       // 打开动作序号：使在途历史加载作废

  const [convs, setConvs] = useState([]);
  const [convTotal, setConvTotal] = useState(0);
  const [convLoading, setConvLoading] = useState(false);

  const endRef = useRef(null);
  const listRef = useRef(null);
  const autoScrollRef = useRef(true);   // 用户上滚后暂停自动滚底，回到底部自动恢复

  const activeState = activeKey ? convStates[activeKey] : null;
  const activeStreaming = !!activeState?.streaming;

  // ── 池基础操作 ──
  const patchConv = useCallback((key, patch) => {
    if (!key) return;
    setConvStates((p) => (p[key] ? { ...p, [key]: { ...p[key], ...patch } } : p));
  }, []);

  const appendMsg = useCallback((key, msg) => {
    if (!key) return;
    setConvStates((p) => {
      const cur = p[key];
      if (!cur) return p;
      return { ...p, [key]: { ...cur, messages: [...cur.messages, msg] } };
    });
  }, []);

  const ensureRt = useCallback((key) => {
    let rt = runtimesRef.current.get(key);
    if (!rt) {
      rt = makeRuntime();
      runtimesRef.current.set(key, rt);
    }
    return rt;
  }, []);

  // ── 流式增量节流（按池分键）：token 进各自池的缓冲，固定间隔合并刷一次 ──
  const stopRtTimer = useCallback((rt) => {
    if (rt.flushTimer) {
      clearInterval(rt.flushTimer);
      rt.flushTimer = null;
    }
  }, []);

  // force=true 时无条件落池（收尾事件）；否则仅激活会话刷渲染，后台会话的
  // 增量暂留缓冲——切回该会话时在 switchActive 里一次性刷出
  const flushDelta = useCallback((key, force = false) => {
    const rt = runtimesRef.current.get(key);
    if (!rt || !rt.deltaBuf) return;
    if (!force && key !== activeKeyRef.current) return;
    const delta = rt.deltaBuf;
    rt.deltaBuf = '';
    setConvStates((p) => {
      const cur = p[key];
      if (!cur) return p;
      const msgs = cur.messages;
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant' && last.streaming) {
        const cp = [...msgs];
        cp[cp.length - 1] = { ...last, content: last.content + delta };
        return { ...p, [key]: { ...cur, messages: cp } };
      }
      return { ...p, [key]: { ...cur, messages: [...msgs, { role: 'assistant', content: delta, streaming: true }] } };
    });
  }, []);

  const queueDelta = useCallback((key, delta) => {
    const rt = ensureRt(key);
    rt.deltaBuf += delta;
    if (!rt.flushTimer) {
      rt.flushTimer = setInterval(() => flushDelta(key), STREAM_FLUSH_MS);
    }
  }, [ensureRt, flushDelta]);

  // 收尾：停计时器并把残余缓冲一次性刷出（后台会话也落池，保证池内容完整）
  const finishStreamText = useCallback((key) => {
    const rt = runtimesRef.current.get(key);
    if (!rt) return;
    stopRtTimer(rt);
    flushDelta(key, true);
  }, [stopRtTimer, flushDelta]);

  const discardStreamText = useCallback((key) => {
    const rt = runtimesRef.current.get(key);
    if (!rt) return;
    stopRtTimer(rt);
    rt.deltaBuf = '';
  }, [stopRtTimer]);

  // 关闭"还在打字"的 AI 气泡：此后再来的 message 增量新开一条，而不是接着追加。
  // 收到 tool_call/confirm/done 等非文本事件时调用（隐式封笔）
  const closeStreamingBubble = useCallback((key) => {
    setConvStates((p) => {
      const cur = p[key];
      if (!cur) return p;
      const msgs = cur.messages;
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant' && last.streaming) {
        const cp = [...msgs];
        cp[cp.length - 1] = { ...last, streaming: false };
        return { ...p, [key]: { ...cur, messages: cp } };
      }
      return p;
    });
  }, []);

  // ── 闲置池 LRU 回收：保护内存，流式中的池与激活池不回收 ──
  const prunePools = useCallback(() => {
    const idle = Object.values(convStatesRef.current)
      .filter((s) => !s.streaming && s.key !== activeKeyRef.current)
      .sort((a, b) => a.lastAccess - b.lastAccess);
    if (idle.length <= MAX_IDLE_POOLS) return;
    const doomed = idle.slice(0, idle.length - MAX_IDLE_POOLS).map((s) => s.key);
    setConvStates((p) => {
      const np = { ...p };
      doomed.forEach((k) => { delete np[k]; });
      return np;
    });
    // 能走到回收的池都已停止生成，runtime 无在途计时器，直接删
    doomed.forEach((k) => runtimesRef.current.delete(k));
  }, []);

  // 切换激活会话：纯视图切换。flush 出该池后台积压的增量，touch LRU，顺手回收
  const switchActive = useCallback((key) => {
    openSeqRef.current += 1;        // 使在途的历史加载作废（打开动作发生竞争）
    activeKeyRef.current = key;
    setActiveKey(key);
    autoScrollRef.current = true;
    setConvStates((p) => (p[key] ? { ...p, [key]: { ...p[key], lastAccess: Date.now() } } : p));
    flushDelta(key);                // 此刻已是激活键，后台积压增量一次刷出
    prunePools();
  }, [flushDelta, prunePools]);

  // 卸载：作废全部在途回调 + 掐断所有 SSE（后端各自兜底存档）+ 清理全部计时器
  useEffect(() => () => {
    runtimesRef.current.forEach((rt) => {
      rt.gen += 1;
      rt.abortCtrl?.abort();
      stopRtTimer(rt);
    });
  }, [stopRtTimer]);

  // 自动滚底只在用户本就位于底部时执行；上滚阅读不被打断
  const handleListScroll = useCallback(() => {
    const el = listRef.current;
    if (!el) return;
    autoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }, []);

  useEffect(() => {
    if (autoScrollRef.current) endRef.current?.scrollIntoView({ block: 'end' });
  }, [activeState?.messages, activeState?.thinking, activeKey]);

  // ── 历史会话 ──
  const refreshConvs = useCallback(async () => {
    try {
      const { conversations, total } = await listConversations(user);
      setConvs(conversations);
      setConvTotal(total);
    } catch (err) {
      if (err.tokenExpired) logout();
    }
  }, [user, logout]);

  useEffect(() => { refreshConvs(); }, [refreshConvs]);

  const loadMoreConvs = useCallback(async () => {
    if (convLoading || convs.length >= convTotal) return;
    setConvLoading(true);
    try {
      const { conversations, total } = await listConversations(user, convs.length);
      setConvs((p) => [...p, ...conversations]);
      setConvTotal(total);
    } catch (err) {
      if (err.tokenExpired) logout();
      else message.error(err.message || '加载历史会话失败');
    } finally {
      setConvLoading(false);
    }
  }, [user, convs.length, convTotal, convLoading, logout]);

  const handleNewChat = useCallback(() => {
    // 当前已是空白新对话就不重复建池
    const cur = convStatesRef.current[activeKeyRef.current];
    if (cur && cur.key.startsWith('tmp:') && !cur.streaming && cur.messages.length === 0) return;
    const key = `tmp:${++tmpSeqRef.current}`;
    setConvStates((p) => ({ ...p, [key]: makeConvState(key) }));
    ensureRt(key);
    switchActive(key);
  }, [ensureRt, switchActive]);

  // 初始进入即一个空白新对话视图
  useEffect(() => { handleNewChat(); }, [handleNewChat]);

  const handleOpenConv = useCallback(async (convId) => {
    // 池命中：流式中的会话直接切回实时视图；近期用过的池免重新拉取
    if (convStatesRef.current[convId]) {
      switchActive(convId);
      return;
    }
    const open = ++openSeqRef.current;
    setConvLoading(true);
    try {
      const { messages: stored, hasOlder: older } = await fetchConversationMessages(user, convId);
      if (open !== openSeqRef.current) return;   // 加载期间又切换/新建了会话
      setConvStates((p) => ({
        ...p,
        [convId]: {
          ...makeConvState(convId),
          messages: mapHistoryMessages(stored),
          hasOlder: older,
          lastAccess: Date.now(),
        },
      }));
      ensureRt(convId);
      switchActive(convId);
    } catch (err) {
      if (err.tokenExpired) logout();
      else message.error(err.message || '读取会话失败');
    } finally {
      setConvLoading(false);
    }
  }, [user, logout, ensureRt, switchActive]);

  // 加载更早消息：按当前首条 seq 向上翻页，前插后保持滚动位置不跳
  const handleLoadOlder = useCallback(async () => {
    const key = activeKeyRef.current;
    const st = key ? convStatesRef.current[key] : null;
    if (!key || !st || st.loadingOlder) return;
    const firstSeq = st.messages.find((m) => m.seq)?.seq;
    if (!firstSeq) return;
    patchConv(key, { loadingOlder: true });
    try {
      const { messages: older, hasOlder: more } = await fetchConversationMessages(user, key, firstSeq);
      if (older.length) {
        const el = listRef.current;
        const prevHeight = el ? el.scrollHeight : 0;
        setConvStates((p) => {
          const cur = p[key];
          if (!cur) return p;
          return { ...p, [key]: { ...cur, messages: [...mapHistoryMessages(older), ...cur.messages] } };
        });
        requestAnimationFrame(() => {
          const el2 = listRef.current;
          if (el2) el2.scrollTop += el2.scrollHeight - prevHeight;
        });
      }
      patchConv(key, { hasOlder: more });
    } catch (err) {
      if (err.tokenExpired) logout();
      else message.error(err.message || '加载更早消息失败');
    } finally {
      patchConv(key, { loadingOlder: false });
    }
  }, [user, logout, patchConv]);

  const handleDeleteConv = useCallback(async (convId) => {
    try {
      await deleteConversation(user, convId);
      const rt = runtimesRef.current.get(convId);
      if (rt) {
        rt.gen += 1;   // 作废该会话在途流回调（若有）
        rt.abortCtrl?.abort();
        stopRtTimer(rt);
        runtimesRef.current.delete(convId);
      }
      setConvStates((p) => {
        if (!p[convId]) return p;
        const np = { ...p };
        delete np[convId];
        return np;
      });
      if (activeKeyRef.current === convId) handleNewChat();   // 删的是当前会话：回到新对话
      refreshConvs();
    } catch (err) {
      if (err.tokenExpired) logout();
      else message.error(err.message || '删除会话失败');
    }
  }, [user, refreshConvs, logout, handleNewChat, stopRtTimer]);

  // 丢弃本地临时会话（未入库/已失败，服务器侧无对应记录，无需调删除接口）
  const handleDiscardTmp = useCallback((key) => {
    setConvStates((p) => {
      if (!p[key]) return p;
      const np = { ...p };
      delete np[key];
      return np;
    });
    const rt = runtimesRef.current.get(key);
    if (rt) {
      rt.gen += 1;
      rt.abortCtrl?.abort();
      stopRtTimer(rt);
    }
    runtimesRef.current.delete(key);
    if (activeKeyRef.current === key) handleNewChat();
  }, [handleNewChat, stopRtTimer]);

  // 新会话 done 回传真实 conv_id：tmp 池整体迁移成真实键，激活指针同步换键
  const migrateTmpPool = useCallback((tmpKey, realKey) => {
    setConvStates((p) => {
      const cur = p[tmpKey];
      if (!cur) return p;
      const np = { ...p };
      // 目标键已有池理论上不可能（conv_key 服务端唯一新建），保守起见以最新内容为准
      np[realKey] = { ...cur, key: realKey };
      delete np[tmpKey];
      return np;
    });
    const rt = runtimesRef.current.get(tmpKey);
    if (rt) {
      runtimesRef.current.delete(tmpKey);
      runtimesRef.current.set(realKey, rt);
    }
    if (activeKeyRef.current === tmpKey) {
      activeKeyRef.current = realKey;
      setActiveKey(realKey);
    }
  }, []);

  const handleSend = useCallback(async () => {
    const key = activeKeyRef.current;
    const st = key ? convStatesRef.current[key] : null;
    const text = (st?.input || '').trim();
    if (!key || !st || !text || st.streaming || convLoading) return;
    const rt = ensureRt(key);
    const gen = ++rt.gen;   // 作废同会话此前的在途回调（停止/删除后重发）

    const running = Object.values(convStatesRef.current).filter((s) => s.streaming).length;
    if (running >= MAX_ACTIVE_STREAMS) {
      message.warning(`最多同时运行 ${MAX_ACTIVE_STREAMS} 个任务，请先停止一个或等待完成`);
      return;
    }

    patchConv(key, { input: '', streaming: true, thinking: true });
    autoScrollRef.current = true;
    appendMsg(key, { role: 'user', content: text });

    rt.abortCtrl = startChatStream(text, key.startsWith('tmp:') ? null : key, user, {
      onThinking: () => { if (gen === rt.gen) patchConv(key, { thinking: true }); },
      onToolCall: (d) => {
        if (gen !== rt.gen) return;
        finishStreamText(key);
        closeStreamingBubble(key);
        patchConv(key, { thinking: false });
        appendMsg(key, { role: 'tool', toolName: d.name, toolArgs: d.arguments });
      },
      onToolResult: (d) => {
        if (gen !== rt.gen) return;
        setConvStates((p) => {
          const cur = p[key];
          if (!cur) return p;
          const cp = [...cur.messages];
          for (let i = cp.length - 1; i >= 0; i--) {
            if (cp[i].role === 'tool' && cp[i].toolName === d.name && !cp[i].toolResult) {
              cp[i] = { ...cp[i], toolResult: d.result };
              break;
            }
          }
          return { ...p, [key]: { ...cur, messages: cp } };
        });
      },
      // 逐 token 增量先进各自池的缓冲，按固定间隔合并渲染（见 STREAM_FLUSH_MS）；
      // 非激活会话的缓冲不刷 state，切回时一次性刷出
      onMessage: (d) => {
        if (gen !== rt.gen) return;
        setConvStates((p) => {
          const cur = p[key];
          if (!cur) return p;
          return cur.thinking ? { ...p, [key]: { ...cur, thinking: false } } : p;
        });
        queueDelta(key, d.delta);
      },
      // 收尾修正（下载链接重签）：整体替换最后一条 AI 气泡（全文已含缓冲内容）
      onMessageReplace: (d) => {
        if (gen !== rt.gen) return;
        discardStreamText(key);
        setConvStates((p) => {
          const cur = p[key];
          if (!cur) return p;
          const last = cur.messages[cur.messages.length - 1];
          if (last && last.role === 'assistant') {
            const cp = [...cur.messages];
            cp[cp.length - 1] = { ...last, content: d.content, streaming: false };
            return { ...p, [key]: { ...cur, messages: cp } };
          }
          return { ...p, [key]: { ...cur, messages: [...cur.messages, { role: 'assistant', content: d.content, streaming: false }] } };
        });
      },
      onConfirmRequired: (d) => {
        if (gen !== rt.gen) return;
        finishStreamText(key);
        closeStreamingBubble(key);
        patchConv(key, { thinking: false, confirmData: d });
      },
      onError: (err) => {
        if (gen !== rt.gen) return;
        // 掐断旧流：后端发 error 之后仍会发 done，不掐断的话旧流的 done
        // 会与本地的停止状态竞态双写
        rt.abortCtrl?.abort();
        discardStreamText(key);
        closeStreamingBubble(key);
        patchConv(key, { streaming: false, thinking: false, confirmData: null });
        if (err.tokenExpired) logout();
        else appendMsg(key, { role: 'system', content: `出错了：${err.message}` });
      },
      onDone: (d) => {
        if (gen !== rt.gen) return;
        finishStreamText(key);
        closeStreamingBubble(key);
        // confirmData 清零兜底：确认等待超时后后端会继续跑完并 done，
        // 悬空的确认单没人消费，不清掉弹窗会一直挂着
        patchConv(key, { streaming: false, thinking: false, confirmData: null });
        if (d.conv_id && key.startsWith('tmp:')) migrateTmpPool(key, d.conv_id);
        refreshConvs();   // 新会话入库/标题更新后刷新侧栏排序
      },
    });
  }, [convLoading, user, logout, refreshConvs, ensureRt, patchConv, appendMsg,
    finishStreamText, discardStreamText, closeStreamingBubble, queueDelta, migrateTmpPool]);

  // 停止指定会话的生成：掐断连接（后端会把已生成的部分落库），可从侧栏远程停止
  const stopConv = useCallback((key) => {
    const rt = runtimesRef.current.get(key);
    if (rt) {
      rt.gen += 1;   // 作废该流后续的 done/error 回调
      rt.abortCtrl?.abort();
    }
    discardStreamText(key);
    closeStreamingBubble(key);
    patchConv(key, { streaming: false, thinking: false, confirmData: null });
    // 稍等后台存档完成再刷新侧栏，让半截回答反映到会话排序里
    setTimeout(() => refreshConvs(), 800);
  }, [discardStreamText, closeStreamingBubble, patchConv, refreshConvs]);

  const handleStop = useCallback(() => {
    if (activeKeyRef.current) stopConv(activeKeyRef.current);
  }, [stopConv]);

  const handleConfirm = useCallback(async (token, decision) => {
    const key = activeKeyRef.current;
    patchConv(key, { confirmData: null });
    try {
      await confirmAction(user, token, decision);
    } catch (err) {
      if (err.tokenExpired) {
        logout();
        return;
      }
      appendMsg(key, { role: 'system', content: `确认失败: ${err.message}` });
    }
  }, [user, appendMsg, logout, patchConv]);

  const renderMessage = (m, i) => {
    if (m.role === 'tool') {
      const r = m.toolResult;
      return (
        <ToolCard key={i}>
          <ToolOutlined style={{ color: '#2563eb' }} />
          <span style={{ fontWeight: 600 }}>{m.toolName}</span>
          {m.toolArgs?.query && <Tag style={{ marginInlineEnd: 0 }}>{m.toolArgs.query}</Tag>}
          {m.toolArgs?.identifier && <Tag style={{ marginInlineEnd: 0 }}>{m.toolArgs.identifier}</Tag>}
          {m.toolArgs?.count && <Tag style={{ marginInlineEnd: 0 }}>{m.toolArgs.count} 条</Tag>}
          {r ? (
            r.error ? <Text type="danger">失败：{r.error}</Text>
              : r.canceled ? <Text type="secondary">已取消</Text>
              : <Text style={{ color: '#16a34a' }}>完成</Text>
          ) : <Spin size="small" />}
        </ToolCard>
      );
    }
    if (m.role === 'system') {
      return <SystemLine key={i}>{m.content}</SystemLine>;
    }
    const isUser = m.role === 'user';
    return (
      <BubbleRow key={i} $isUser={isUser}>
        <AvatarWrap>
          <Avatar
            size={30}
            style={isUser
              ? { backgroundColor: '#e5e9f0', color: '#4b5563', fontWeight: 600 }
              : { background: 'linear-gradient(135deg, #2563eb, #7c3aed)' }}
            icon={!isUser ? <RobotOutlined /> : undefined}
          >
            {isUser ? (user?.username || '?').slice(0, 1).toUpperCase() : undefined}
          </Avatar>
        </AvatarWrap>
        <Bubble $isUser={isUser}>{isUser ? m.content : renderRich(m.content)}</Bubble>
      </BubbleRow>
    );
  };

  // 侧栏顶部的本地临时会话条目：尚未入库（生成中）或入库失败（error 未迁移）的会话。
  // 不展示空白 tmp 池——它就是当前视图，侧栏出现一排"新对话"只有噪音
  const tmpEntries = Object.values(convStates).filter(
    (s) => s.key.startsWith('tmp:') && (s.streaming || s.messages.length > 0)
  );
  const tmpTitle = (s) => {
    const first = s.messages.find((m) => m.role === 'user');
    return (first?.content || '新对话').replace(/\s+/g, ' ').slice(0, 20);
  };

  return (
    <Card style={{ borderRadius: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Avatar size={34} style={{ background: 'linear-gradient(135deg, #2563eb, #7c3aed)' }} icon={<RobotOutlined />} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 15 }}>AI 文件助手</div>
            <Text type="secondary" style={{ fontSize: 12 }}>语义搜索 · 文件管理 · 危险操作确认</Text>
          </div>
        </div>
        <Tooltip title="新建对话（生成中的对话可在后台继续运行）">
          <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat} />
        </Tooltip>
      </div>

      <ChatLayout>
        <ConvSidebar>
          {tmpEntries.length === 0 && convs.length === 0 && !convLoading && (
            <Text type="secondary" style={{ fontSize: 12, textAlign: 'center', padding: '12px 0' }}>
              暂无历史会话
            </Text>
          )}
          {tmpEntries.map((s) => (
            <ConvItem key={s.key} $active={s.key === activeKey} onClick={() => switchActive(s.key)}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {tmpTitle(s)}
                </div>
                <div style={{ fontSize: 11, color: '#8c95a3', marginTop: 2 }}>
                  {s.streaming ? '生成中...' : '未保存'}
                </div>
              </div>
              <ConvStatus>
                {s.confirmData && (
                  <Tooltip title="该会话有操作待确认，点击进入处理">
                    <WarningOutlined style={{ color: '#faad14', fontSize: 13 }} />
                  </Tooltip>
                )}
                {s.streaming ? (
                  <>
                    <Spin size="small" />
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<StopOutlined />}
                      onClick={(e) => { e.stopPropagation(); stopConv(s.key); }}
                    />
                  </>
                ) : (
                  <Button
                    className="del-btn"
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={(e) => { e.stopPropagation(); handleDiscardTmp(s.key); }}
                  />
                )}
              </ConvStatus>
            </ConvItem>
          ))}
          {convs.map((c) => {
            const pool = convStates[c.conv_id];
            return (
              <ConvItem
                key={c.conv_id}
                $active={c.conv_id === activeKey}
                onClick={() => handleOpenConv(c.conv_id)}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {c.title}
                  </div>
                  <div style={{ fontSize: 11, color: '#8c95a3', marginTop: 2 }}>
                    {c.update_time} · {c.user_message_count} 条
                  </div>
                </div>
                <ConvStatus>
                  {pool?.confirmData && (
                    <Tooltip title="该会话有操作待确认，点击进入处理">
                      <WarningOutlined style={{ color: '#faad14', fontSize: 13 }} />
                    </Tooltip>
                  )}
                  {pool?.streaming ? (
                    <>
                      <Tooltip title="生成中，点击会话查看进度">
                        <Spin size="small" />
                      </Tooltip>
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<StopOutlined />}
                        onClick={(e) => { e.stopPropagation(); stopConv(c.conv_id); }}
                      />
                    </>
                  ) : (
                    <Popconfirm
                        title="删除该会话？"
                        description="删除后将从历史列表移除"
                        onConfirm={() => handleDeleteConv(c.conv_id)}
                        okText="删除"
                        cancelText="取消"
                      >
                        <Button
                          className="del-btn"
                          type="text"
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </Popconfirm>
                  )}
                </ConvStatus>
              </ConvItem>
            );
          })}
          {convs.length < convTotal && (
            <Button size="small" type="link" block onClick={loadMoreConvs} loading={convLoading}>
              加载更多（{convs.length}/{convTotal}）
            </Button>
          )}
        </ConvSidebar>

        <div style={{ flex: 1, minWidth: 0 }}>
          <MsgList ref={listRef} onScroll={handleListScroll}>
            {activeState?.hasOlder && (
              <Button size="small" type="link" onClick={handleLoadOlder} loading={activeState.loadingOlder}>
                加载更早消息
              </Button>
            )}
            {(!activeState || activeState.messages.length === 0) && !activeState?.thinking && (
          <div style={{ marginTop: 48 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<Text type="secondary">描述你想找的文件，或让我帮你管理文件</Text>}
            >
              <SuggestBox>
                {SUGGESTIONS.map((s) => (
                  <Tag.CheckableTag
                    key={s}
                    checked={false}
                    onChange={() => patchConv(activeKey, { input: s })}
                    style={{ border: '1px solid #d9dde5', padding: '2px 10px', borderRadius: 12 }}
                  >
                    {s}
                  </Tag.CheckableTag>
                ))}
              </SuggestBox>
            </Empty>
          </div>
        )}
        {activeState?.messages.map(renderMessage)}
        {activeState?.thinking && (
          <ThinkingRow>
            <Spin size="small" />
            <span>AI 正在思考...</span>
          </ThinkingRow>
        )}
        <div ref={endRef} />
      </MsgList>

      <InputBar>
        <Input.TextArea
          value={activeState?.input || ''}
          onChange={(e) => patchConv(activeKey, { input: e.target.value })}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder="用自然语言描述你想找的文件，或让我统计、分享、删除文件..."
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={activeStreaming || convLoading}
          style={{ flex: 1 }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          {activeStreaming ? (
            <Button
              danger
              icon={<StopOutlined />}
              onClick={handleStop}
              block
            >
              停止
            </Button>
          ) : (
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={handleSend}
              disabled={!activeState?.input?.trim() || convLoading}
              block
            >
              发送
            </Button>
          )}
        </div>
        <Hint>Enter 发送 · Shift+Enter 换行 · 切换会话不打断生成，可多会话并行</Hint>
      </InputBar>
        </div>
      </ChatLayout>

      <ConfirmModal
        open={!!activeState?.confirmData}
        data={activeState?.confirmData}
        onConfirm={handleConfirm}
        onCancel={handleConfirm}
      />
    </Card>
  );
}
