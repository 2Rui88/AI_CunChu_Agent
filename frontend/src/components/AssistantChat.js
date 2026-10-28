import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Card, Input, Button, Avatar, Tooltip, Typography, Spin, Empty, Tag, Popconfirm } from 'antd';
import {
  SendOutlined, PlusOutlined, RobotOutlined, ToolOutlined, DeleteOutlined, StopOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import ConfirmModal from './ConfirmModal';
import {
  startChatStream, confirmAction, listConversations, fetchConversationMessages, deleteConversation,
} from '../services/agent';
import { useAuth } from '../contexts/AuthContext';

const { Text } = Typography;

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
 */
const mapHistoryMessages = (msgs) => {
  const toolNameById = {};
  const out = [];
  msgs.forEach((m) => {
    if (m.role === 'user') {
      out.push({ role: 'user', content: m.content });
    } else if (m.role === 'assistant') {
      (m.tool_calls || []).forEach((tc) => {
        toolNameById[tc.id] = tc.function?.name;
      });
      if (m.content) out.push({ role: 'assistant', content: m.content });
    } else if (m.role === 'tool') {
      let result = null;
      try { result = JSON.parse(m.content); } catch { result = null; }
      out.push({
        role: 'tool',
        toolName: toolNameById[m.tool_call_id] || '工具调用',
        toolArgs: {},
        toolResult: result,
      });
    }
  });
  return out;
};

/**
 * AI 文件助手（首页主体，全站唯一的文件搜索入口）
 * 找文件直接在对话里描述内容即可；助手同时负责文件管理操作与危险操作确认。
 * 对话历史持久化在服务端（MySQL），左侧栏可查看/恢复/删除历史会话。
 */
export default function AssistantChat() {
  const { user, logout } = useAuth();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [currentConvId, setCurrentConvId] = useState(null);
  const [confirmData, setConfirmData] = useState(null);
  const [convs, setConvs] = useState([]);
  const abortRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking]);

  const addMsg = useCallback((msg) => setMessages((p) => [...p, msg]), []);

  // 关闭"还在打字"的 AI 气泡：此后再来的 message 增量新开一条，而不是接着追加。
  // 收到 tool_call/confirm/done 等非文本事件时调用（隐式封笔）
  const closeStreamingBubble = useCallback(() => {
    setMessages((p) => {
      const last = p[p.length - 1];
      if (last && last.role === 'assistant' && last.streaming) {
        const cp = [...p];
        cp[cp.length - 1] = { ...last, streaming: false };
        return cp;
      }
      return p;
    });
  }, []);

  // ── 历史会话 ──
  const refreshConvs = useCallback(async () => {
    try {
      const { conversations } = await listConversations(user);
      setConvs(conversations);
    } catch (err) {
      if (err.tokenExpired) logout();
    }
  }, [user, logout]);

  useEffect(() => { refreshConvs(); }, [refreshConvs]);

  const handleOpenConv = useCallback(async (convId) => {
    if (streaming || convId === currentConvId) return;
    abortRef.current?.abort();
    try {
      const { messages: stored } = await fetchConversationMessages(user, convId);
      setMessages(mapHistoryMessages(stored));
      setCurrentConvId(convId);
      setThinking(false);
      setConfirmData(null);
    } catch (err) {
      if (err.tokenExpired) logout();
    }
  }, [streaming, currentConvId, user, logout]);

  const handleDeleteConv = useCallback(async (convId) => {
    try {
      await deleteConversation(user, convId);
      if (convId === currentConvId) {
        // 删的是当前会话：回到新对话状态
        abortRef.current?.abort();
        setMessages([]);
        setCurrentConvId(null);
        setStreaming(false);
        setThinking(false);
        setConfirmData(null);
      }
      refreshConvs();
    } catch (err) {
      if (err.tokenExpired) logout();
    }
  }, [user, currentConvId, refreshConvs, logout]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput('');
    setStreaming(true);
    setThinking(true);
    addMsg({ role: 'user', content: text });

    abortRef.current = startChatStream(text, currentConvId, user, {
      onThinking: () => setThinking(true),
      onToolCall: (d) => {
        setThinking(false);
        closeStreamingBubble();
        addMsg({ role: 'tool', toolName: d.name, toolArgs: d.arguments });
      },
      onToolResult: (d) => {
        setMessages((p) => {
          const cp = [...p];
          for (let i = cp.length - 1; i >= 0; i--) {
            if (cp[i].role === 'tool' && cp[i].toolName === d.name && !cp[i].toolResult) {
              cp[i] = { ...cp[i], toolResult: d.result };
              break;
            }
          }
          return cp;
        });
      },
      // 逐 token 增量：末尾是"还在打字"的 AI 气泡就追加，否则新开一条
      onMessage: (d) => {
        setThinking(false);
        setMessages((p) => {
          const last = p[p.length - 1];
          if (last && last.role === 'assistant' && last.streaming) {
            const cp = [...p];
            cp[cp.length - 1] = { ...last, content: last.content + d.delta };
            return cp;
          }
          return [...p, { role: 'assistant', content: d.delta, streaming: true }];
        });
      },
      // 收尾修正（下载链接重签）：整体替换最后一条 AI 气泡
      onMessageReplace: (d) => {
        setMessages((p) => {
          const last = p[p.length - 1];
          if (last && last.role === 'assistant') {
            const cp = [...p];
            cp[cp.length - 1] = { ...last, content: d.content, streaming: false };
            return cp;
          }
          return [...p, { role: 'assistant', content: d.content, streaming: false }];
        });
      },
      onConfirmRequired: (d) => {
        setThinking(false);
        closeStreamingBubble();
        setConfirmData(d);
      },
      onError: (err) => {
        setThinking(false);
        setStreaming(false);
        closeStreamingBubble();
        if (err.tokenExpired) logout();
        else addMsg({ role: 'system', content: `出错了：${err.message}` });
      },
      onDone: (d) => {
        setThinking(false);
        setStreaming(false);
        closeStreamingBubble();
        if (d.conv_id) setCurrentConvId(d.conv_id);
        refreshConvs();  // 新会话入库/标题更新后刷新侧栏排序
      },
    });
  }, [input, streaming, currentConvId, user, addMsg, closeStreamingBubble, logout, refreshConvs]);

  // 停止生成：掐断连接（后端会把已生成的部分落库），界面立即恢复可输入
  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    closeStreamingBubble();
    setStreaming(false);
    setThinking(false);
    // 稍等后台存档完成再刷新侧栏，让半截回答反映到会话排序里
    setTimeout(() => refreshConvs(), 800);
  }, [closeStreamingBubble, refreshConvs]);

  const handleConfirm = useCallback(async (token, decision) => {
    setConfirmData(null);
    try {
      await confirmAction(user, token, decision);
    } catch (err) {
      addMsg({ role: 'system', content: `确认失败: ${err.message}` });
    }
  }, [user, addMsg]);

  const handleNewChat = () => {
    abortRef.current?.abort();
    setMessages([]);
    setCurrentConvId(null);
    setStreaming(false);
    setThinking(false);
    setConfirmData(null);
  };

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
        <Tooltip title="新建对话">
          <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat} disabled={streaming} />
        </Tooltip>
      </div>

      <ChatLayout>
        <ConvSidebar>
          {convs.length === 0 && (
            <Text type="secondary" style={{ fontSize: 12, textAlign: 'center', padding: '12px 0' }}>
              暂无历史会话
            </Text>
          )}
          {convs.map((c) => (
            <ConvItem
              key={c.conv_id}
              $active={c.conv_id === currentConvId}
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
            </ConvItem>
          ))}
        </ConvSidebar>

        <div style={{ flex: 1, minWidth: 0 }}>
          <MsgList>
            {messages.length === 0 && !thinking && (
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
                    onChange={() => setInput(s)}
                    style={{ border: '1px solid #d9dde5', padding: '2px 10px', borderRadius: 12 }}
                  >
                    {s}
                  </Tag.CheckableTag>
                ))}
              </SuggestBox>
            </Empty>
          </div>
        )}
        {messages.map(renderMessage)}
        {thinking && (
          <ThinkingRow>
            <Spin size="small" />
            <span>AI 正在思考...</span>
          </ThinkingRow>
        )}
        <div ref={endRef} />
      </MsgList>

      <InputBar>
        <Input.TextArea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder="用自然语言描述你想找的文件，或让我统计、分享、删除文件..."
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={streaming}
          style={{ flex: 1 }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          {streaming ? (
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
              disabled={!input.trim()}
              block
            >
              发送
            </Button>
          )}
        </div>
        <Hint>Enter 发送 · Shift+Enter 换行 · 删除/分享操作会弹窗确认</Hint>
      </InputBar>
        </div>
      </ChatLayout>

      <ConfirmModal
        open={!!confirmData}
        data={confirmData}
        onConfirm={handleConfirm}
        onCancel={handleConfirm}
      />
    </Card>
  );
}
