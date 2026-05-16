import { API_CONFIG } from '../config';

const AGENT_BASE = `${API_CONFIG.BASE_URL}/api/agent`;

/**
 * 启动 SSE 对话流
 * @returns {AbortController} 用于中断连接
 */
export const startChatStream = (message, convId, user, apiKey, callbacks) => {
  const { onThinking, onToolCall, onToolResult, onMessage, onConfirmRequired, onError, onDone } = callbacks;
  const controller = new AbortController();

  fetch(`${AGENT_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user: user.username,
      token: user.token,
      api_key: apiKey,
      message,
      conversation_id: convId || null,
    }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (response.status === 401) {
        const err = new Error('token expired');
        err.tokenExpired = true;
        onError?.(err);
        return;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        onError?.(new Error(data.msg || `HTTP ${response.status}`));
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              switch (currentEvent) {
                case 'thinking': onThinking?.(data); break;
                case 'tool_call': onToolCall?.(data); break;
                case 'tool_result': onToolResult?.(data); break;
                case 'message': onMessage?.(data); break;
                case 'confirm_required': onConfirmRequired?.(data); break;
                case 'error': onError?.(new Error(data.message)); break;
                case 'done': onDone?.(data); break;
                default: break;
              }
            } catch { /* ignore parse errors */ }
          }
        }
      }
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError?.(err);
    });

  return controller;
};

/** 确认或拒绝危险操作 */
export const confirmAction = async (user, confirmationToken, decision) => {
  const resp = await fetch(`${AGENT_BASE}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user: user.username, token: user.token,
      confirmation_token: confirmationToken, decision,
    }),
  });
  const data = await resp.json();
  if (data.code !== 0) throw new Error(data.msg || 'Confirmation failed');
  return data;
};
