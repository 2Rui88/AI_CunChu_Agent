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
    SHARE_PIC: '/api/sharepic',
    CHUNK_INIT: '/api/chunk_init',
    CHUNK_UPLOAD: '/api/chunk_upload',
    CHUNK_MERGE: '/api/chunk_merge',
    AI: '/api/ai',
    AGENT: '/api/agent',
  },
  CHUNK_SIZE: 10 * 1024 * 1024,        // 10MB per chunk
  CHUNK_THRESHOLD: 10 * 1024 * 1024,   // files > 10MB use chunked upload
};

export const STORAGE_KEYS = {
  API_KEY_PREFIX: 'dashscope_api_key_',
  USER: 'yuncunchu_user',
};
