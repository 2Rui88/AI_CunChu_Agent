import React, { useState, useRef, useEffect, useCallback } from 'react';
import { Input, Button, Typography, Spin, Empty } from 'antd';
import { SendOutlined, DeleteOutlined, RobotOutlined } from '@ant-design/icons';
import styled from '@emotion/styled';
import ConfirmModal from './ConfirmModal';
import { startChatStream, confirmAction } from '../services/agent';
import { fetchApiKey } from '../services/ai';
import { useAuth } from '../contexts/AuthContext';

const { Text } = Typography;

const PanelContainer = styled.div`
  position: fixed; right: 0; top: 0; bottom: 0; width: 420px; max-width: 90vw;
  background: rgba(255,255,255,0.96); backdrop-filter: blur(12px);
  border-left: 1px solid #e8e8e8; display: flex; flex-direction: column;
  z-index: 1000; box-shadow: -4px 0 24px rgba(0,0,0,0.08);
`;
const Header = styled.div`
  padding: 14px 16px; border-bottom: 1px solid #f0f0f0;
  display: flex; align-items: center; justify-content: space-between;
`;
const MessageList = styled.div`
  flex: 1; overflow-y: auto; padding: 12px 16px;
  display: flex; flex-direction: column; gap: 12px;
`;
const InputArea = styled.div`
  padding: 10px 14px; border-top: 1px solid #f0f0f0; display: flex; gap: 8px;
`;

const Bubble = styled.div`
  max-width: 85%; padding: 10px 14px; border-radius: 12px; font-size: 14px;
  line-height: 1.6; white-space: pre-wrap;
  background: ${(p) => (p.$isUser ? '#e6f7ff' : '#f6ffed')};
  ${(p) => (p.$isUser ? 'margin-left: auto;' : '')}
`;
const ToolTag = styled.span`
  display: inline-block; background: #fafafa; border: 1px solid #d9d9d9;
  border-radius: 4px; padding: 2px 8px; margin: 2px; font-size: 12px; color: #666;
`;
const ThinkingRow = styled.div`
  display: flex; align-items: center; gap: 8px; color: #999; font-size: 13px;
`;

export default function ChatPanel({ open, onClose }) {
  const { user, logout } = useAuth();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [currentConvId, setCurrentConvId] = useState(null);
  const [confirmData, setConfirmData] = useState(null);
  const [apiKey, setApiKey] = useState('');
  const abortRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => { fetchApiKey(user).then(setApiKey); }, [user]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, thinking]);

  const addMsg = useCallback((msg) => setMessages((p) => [...p, msg]), []);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;
    if (!apiKey) { addMsg({ role: 'system', content: '请先在首页设置 DashScope API Key' }); return; }
    setInput(''); setStreaming(true); setThinking(true);
    addMsg({ role: 'user', content: text });

    abortRef.current = startChatStream(text, currentConvId, user, apiKey, {
      onThinking: () => setThinking(true),
      onToolCall: (d) => { setThinking(false); addMsg({ role: 'tool', toolName: d.name, toolArgs: d.arguments }); },
      onToolResult: (d) => {
        setMessages((p) => {
          const cp = [...p];
          for (let i = cp.length - 1; i >= 0; i--) {
            if (cp[i].role === 'tool' && cp[i].toolName === d.name) {
              cp[i] = { ...cp[i], toolResult: d.result }; break;
            }
          }
          return cp;
        });
      },
      onMessage: (d) => { setThinking(false); addMsg({ role: 'assistant', content: d.delta }); },
      onConfirmRequired: (d) => { setThinking(false); setConfirmData(d); },
      onError: (err) => { setThinking(false); setStreaming(false); if (err.tokenExpired) logout(); else addMsg({ role: 'system', content: `错误: ${err.message}` }); },
      onDone: (d) => { setThinking(false); setStreaming(false); if (d.conv_id) setCurrentConvId(d.conv_id); },
    });
  }, [input, streaming, apiKey, currentConvId, user, addMsg, logout]);

  const handleConfirm = useCallback(async (token, decision) => {
    setConfirmData(null);
    try { await confirmAction(user, token, decision); }
    catch (err) { addMsg({ role: 'system', content: `确认失败: ${err.message}` }); }
  }, [user, addMsg]);

  const handleNewChat = () => { abortRef.current?.abort(); setMessages([]); setCurrentConvId(null); setStreaming(false); setThinking(false); setConfirmData(null); };

  if (!open) return null;

  return (
    <PanelContainer>
      <Header>
        <Text strong style={{ fontSize: 16 }}><RobotOutlined style={{ marginRight: 6 }} />AI 文件助手</Text>
        <div style={{ display: 'flex', gap: 4 }}>
          <Button type="text" icon={<DeleteOutlined />} onClick={handleNewChat} disabled={streaming} title="新建对话" />
          <Button type="text" onClick={onClose}>✕</Button>
        </div>
      </Header>

      <MessageList>
        {messages.length === 0 && !thinking && (
          <Empty description="问我关于你文件的任何问题" style={{ marginTop: 60 }}>
            <Text type="secondary" style={{ fontSize: 13 }}>试试: "帮我找一下猫的照片" 或 "最近上传了什么文件"</Text>
          </Empty>
        )}
        {messages.map((m, i) => (
          <div key={i}>
            {m.role === 'tool' ? (
              <Bubble $isUser={false}>
                <ToolTag>🔧 {m.toolName}</ToolTag>
                {m.toolResult ? (
                  m.toolResult.error ? <Text type="danger" style={{ fontSize: 12 }}>{m.toolResult.error}</Text> :
                  m.toolResult.canceled ? <Text type="secondary" style={{ fontSize: 12 }}>已取消</Text> :
                  <Text type="secondary" style={{ fontSize: 12 }}>完成</Text>
                ) : <Spin size="small" style={{ marginLeft: 4 }} />}
              </Bubble>
            ) : m.role === 'system' ? (
              <Text type="secondary" style={{ textAlign: 'center', fontSize: 13 }}>{m.content}</Text>
            ) : (
              <Bubble $isUser={m.role === 'user'}>{m.content}</Bubble>
            )}
          </div>
        ))}
        {thinking && <ThinkingRow><Spin size="small" /><Text type="secondary">AI 正在思考...</Text></ThinkingRow>}
        <div ref={endRef} />
      </MessageList>

      <InputArea>
        <Input.TextArea value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
          placeholder="输入消息... (Enter 发送)" autoSize={{ minRows: 1, maxRows: 4 }}
          disabled={streaming} style={{ flex: 1 }} />
        <Button type="primary" icon={<SendOutlined />} onClick={handleSend} loading={streaming} disabled={!input.trim() || streaming} />
      </InputArea>

      <ConfirmModal open={!!confirmData} data={confirmData} onConfirm={handleConfirm} onCancel={handleConfirm} />
    </PanelContainer>
  );
}
