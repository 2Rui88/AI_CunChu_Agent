/**
 * RAG 检索评估服务 —— 数据集管理 + 评估运行（逐题同步推进，无后台任务）
 */
import { API_CONFIG } from '../config';

const BASE = API_CONFIG.BASE_URL;
const E = API_CONFIG.ENDPOINTS;

/** 统一 POST JSON，token 过期时抛出 tokenExpired 错误 */
const post = async (url, body) => {
  const resp = await fetch(url, {
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
  if (data.code !== 0) throw new Error(data.msg || '请求失败');
  return data;
};

const authBody = (user) => ({ user: user.username, token: user.token });

/** 上传数据集（JSONL 文本），返回 { dataset_id, item_count, has_gold, has_gold_answer } */
export const uploadDataset = async (user, name, description, content) => {
  const data = await post(`${BASE}${E.EVAL_DATASET_UPLOAD}`, {
    ...authBody(user), name, description, content,
  });
  return data;
};

/** 数据集列表 */
export const fetchDatasets = async (user) => {
  const data = await post(`${BASE}${E.EVAL_DATASET_LIST}`, authBody(user));
  return data.datasets || [];
};

/** 数据集题目明细 */
export const fetchDatasetItems = async (user, datasetId) => {
  const data = await post(`${BASE}${E.EVAL_DATASET_ITEMS}`, {
    ...authBody(user), dataset_id: datasetId,
  });
  return data.items || [];
};

/** 删除数据集（连同其全部评估运行记录） */
export const deleteDataset = (user, datasetId) =>
  post(`${BASE}${E.EVAL_DATASET_DELETE}`, { ...authBody(user), dataset_id: datasetId });

/** 创建评估运行（不执行；逐题推进由 stepRun 完成） */
export const startRun = async (user, datasetId, topK) => {
  const data = await post(`${BASE}${E.EVAL_RUN_START}`, {
    ...authBody(user), dataset_id: datasetId, top_k: topK,
  });
  return data;
};

/**
 * 执行下一题（同步跑一次真实检索链路）。
 * 返回 { done, completed, total, item, metrics }——done=true 时 metrics 为聚合结果。
 */
export const stepRun = (user, runId) =>
  post(`${BASE}${E.EVAL_RUN_STEP}`, { ...authBody(user), run_id: runId });

/** 运行历史列表 */
export const fetchRuns = async (user) => {
  const data = await post(`${BASE}${E.EVAL_RUN_LIST}`, authBody(user));
  return data.runs || [];
};

/** 运行详情（含逐题结果） */
export const fetchRunDetail = async (user, runId) => {
  const data = await post(`${BASE}${E.EVAL_RUN_DETAIL}`, {
    ...authBody(user), run_id: runId,
  });
  return data;
};

/** 删除运行记录 */
export const deleteRun = (user, runId) =>
  post(`${BASE}${E.EVAL_RUN_DELETE}`, { ...authBody(user), run_id: runId });
