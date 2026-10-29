import { API_CONFIG } from '../config';

const AGENT_BASE = `${API_CONFIG.BASE_URL}/api/agent`;

/**
 * 启动 SSE 对话流（AI 密钥由服务端统一配置，前端不携带）
 * @returns {AbortController} 用于中断连接
 */
export const startChatStream = (message, convId, user, callbacks) => {
  const { onThinking, onToolCall, onToolResult, onMessage, onMessageReplace, onConfirmRequired, onError, onDone } = callbacks;
  const controller = new AbortController();

  fetch(`${AGENT_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      user: user.username,
      token: user.token,
      message,
      conversation_id: convId || null,
    }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (response.status === 401) {
        const err = new Error('登录已过期');
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

      const dispatch = (line) => {
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
              case 'message_replace': onMessageReplace?.(data); break;
              case 'confirm_required': onConfirmRequired?.(data); break;
              case 'error': {
                // 后端 SSE 没有 HTTP 状态码可承载 401，token 失效经 error
                // 事件的 code=4 传达（与 JSON 接口的 code===4 约定一致）
                const err = new Error(data.message || '服务异常');
                if (data.code === 4) err.tokenExpired = true;
                onError?.(err);
                break;
              }
              case 'done': onDone?.(data); break;
              default: break;
            }
          } catch { /* ignore parse errors */ }
        }
      };

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          lines.forEach(dispatch);
        }
        // 流末尾可能残留不带换行的最后一行（连接在事件边界被切断）
        buffer += decoder.decode();
        if (buffer) dispatch(buffer);
      } finally {
        // 异常路径（回调抛错/中途取消）也释放 reader，避免底层连接悬着
        try { reader.cancel(); } catch { /* already closed */ }
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
  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`服务响应异常 (HTTP ${resp.status})`);
  }
  if (data.code === 4) {
    const err = new Error('登录已过期');
    err.tokenExpired = true;
    throw err;
  }
  if (data.code !== 0) throw new Error(data.msg || '确认失败');
  return data;
};

/** 统一 POST JSON，token 过期时抛出 tokenExpired 错误 */
const post = async (path, body) => {
  const resp = await fetch(`${AGENT_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await resp.json();
  } catch {
    throw new Error(`服务响应异常 (HTTP ${resp.status})`);
  }
  if (data.code === 4) {
    const err = new Error('登录已过期');
    err.tokenExpired = true;
    throw err;
  }
  return data;
};

/**
 * 历史会话列表（按最近更新倒序）
 * 返回: { conversations: [{conv_id, title, message_count, update_time}], total }
 */
export const listConversations = async (user, start = 0, count = 30) => {
  const data = await post('/conversations', {
    user: user.username, token: user.token, start, count,
  });
  if (data.code !== 0) throw new Error(data.msg || '获取历史会话失败');
  return { conversations: data.conversations || [], total: data.total || 0 };
};

/**
 * 读取会话消息（正序；传 beforeSeq 取更早一页）
 * 返回: { conversation, messages, has_older }
 */
export const fetchConversationMessages = async (user, convId, beforeSeq = null, count = 50) => {
  const data = await post('/conversations/messages', {
    user: user.username, token: user.token,
    conv_id: convId, before_seq: beforeSeq, count,
  });
  if (data.code !== 0) throw new Error(data.msg || '读取会话消息失败');
  return {
    conversation: data.conversation || {},
    messages: data.messages || [],
    hasOlder: !!data.has_older,
  };
};

/** 软删除会话 */
export const deleteConversation = async (user, convId) => {
  const data = await post('/conversations/delete', {
    user: user.username, token: user.token, conv_id: convId,
  });
  if (data.code !== 0) throw new Error(data.msg || '删除会话失败');
  return data;
};
