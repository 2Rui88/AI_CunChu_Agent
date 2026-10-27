import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Drawer, Input, Button, Avatar, Tooltip, Typography, Spin, Empty, Tag } from 'antd';
import {
  SendOutlined, PlusOutlined, RobotOutlined, ToolOutlined,
} from '@ant-design/icons';
import styled from '@emotion/styled';
import ConfirmModal from './ConfirmModal';
import { startChatStream, confirmAction } from '../services/agent';
import { useAuth } from '../contexts/AuthContext';

const { Text } = Typography;

const MsgList = styled.div`
  flex: 1;
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

const SUGGESTIONS = [
  '帮我找一下猫的照片',
  '最近上传了什么文件',
  '我的存储空间用了多少',
];

export default function ChatPanel({ open, onClose }) {
  const { user, logout } = useAuth();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [currentConvId, setCurrentConvId] = useState(null);
  const [confirmData, setConfirmData] = useState(null);
  const abortRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking]);

  const addMsg = useCallback((msg) => setMessages((p) => [...p, msg]), []);

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
      onMessage: (d) => {
        setThinking(false);
        addMsg({ role: 'assistant', content: d.delta });
      },
      onConfirmRequired: (d) => {
        setThinking(false);
        setConfirmData(d);
      },
      onError: (err) => {
        setThinking(false);
        setStreaming(false);
        if (err.tokenExpired) logout();
        else addMsg({ role: 'system', content: `出错了：${err.message}` });
      },
      onDone: (d) => {
        setThinking(false);
        setStreaming(false);
        if (d.conv_id) setCurrentConvId(d.conv_id);
      },
    });
  }, [input, streaming, currentConvId, user, addMsg, logout]);

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
          {r ? (
            r.error ? <Text type="danger">失败：{r.error}</Text>
              : r.canceled ? <Text type="secondary">已取消</Text>
              : <Text type="success" style={{ color: '#16a34a' }}>完成</Text>
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
        <Bubble $isUser={isUser}>{m.content}</Bubble>
      </BubbleRow>
    );
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width={432}
      mask={false}
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <Avatar size={34} style={{ background: 'linear-gradient(135deg, #2563eb, #7c3aed)' }} icon={<RobotOutlined />} />
          <div>
            <div style={{ fontWeight: 700, fontSize: 15 }}>AI 文件助手</div>
            <Text type="secondary" style={{ fontSize: 12 }}>语义搜索 · 工具调用 · 危险操作确认</Text>
          </div>
        </div>
      }
      extra={
        <Tooltip title="新建对话">
          <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat} disabled={streaming} />
        </Tooltip>
      }
      styles={{ body: { padding: 0, display: 'flex', flexDirection: 'column', background: '#f7f9fc' } }}
    >
      <MsgList>
        {messages.length === 0 && !thinking && (
          <div style={{ marginTop: 48 }}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={<Text type="secondary">问我关于你文件的任何问题</Text>}
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
          placeholder="输入消息，例如：帮我找上个月的技术方案"
          autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={streaming}
          style={{ flex: 1 }}
        />
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={handleSend}
            loading={streaming}
            disabled={!input.trim()}
            block
          >
            发送
          </Button>
        </div>
        <Hint>Enter 发送 · Shift+Enter 换行 · 删除/分享操作会弹窗确认</Hint>
      </InputBar>

      <ConfirmModal
        open={!!confirmData}
        data={confirmData}
        onConfirm={handleConfirm}
        onCancel={handleConfirm}
      />
    </Drawer>
  );
}
