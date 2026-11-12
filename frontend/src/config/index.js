export const API_CONFIG = {
  BASE_URL: '',
  ENDPOINTS: {
    LOGIN: '/api/login',
    REGISTER: '/api/reg',
    MY_FILES: '/api/myfiles',
    MD5: '/api/md5',
    UPLOAD: '/api/upload',
    DEAL_FILE: '/api/dealfile',
    DEAL_SHARE_FILE: '/api/dealsharefile',
    SHARE_FILES: '/api/sharefiles',
    DEPT_FILES: '/api/dept/files',
    CHUNK_INIT: '/api/chunk_init',
    CHUNK_UPLOAD: '/api/chunk_upload',
    CHUNK_MERGE: '/api/chunk_merge',
    AI: '/api/ai',
    AGENT: '/api/agent',
    VECTOR_DOCS: '/api/vector/docs',
    VECTOR_CHUNKS: '/api/vector/chunks',
    EVAL_DATASET_UPLOAD: '/api/eval/dataset/upload',
    EVAL_DATASET_LIST: '/api/eval/dataset/list',
    EVAL_DATASET_ITEMS: '/api/eval/dataset/items',
    EVAL_DATASET_DELETE: '/api/eval/dataset/delete',
    EVAL_RUN_START: '/api/eval/run/start',
    EVAL_RUN_STEP: '/api/eval/run/step',
    EVAL_RUN_LIST: '/api/eval/run/list',
    EVAL_RUN_DETAIL: '/api/eval/run/detail',
    EVAL_RUN_DELETE: '/api/eval/run/delete',
  },
  CHUNK_SIZE: 10 * 1024 * 1024,        // 10MB per chunk
  CHUNK_THRESHOLD: 10 * 1024 * 1024,   // files > 10MB use chunked upload
  // 内容索引体积门槛（与后端 config.py 的 parse_max_mb 保持一致）：
  // 超过该体积的文件只建文件名索引，由用户在文件列表手动触发"建立内容索引"
  CONTENT_INDEX_LIMIT: 100 * 1024 * 1024,
};

export const STORAGE_KEYS = {
  USER: 'yuncunchu_user',
};
