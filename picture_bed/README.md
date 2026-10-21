# ⚠️ 此目录已弃用（stale copy）

`picture_bed/` 是旧版图床前端的残留拷贝，**不再维护、无法构建运行**：

- 缺少 `package.json`、`index.html`、`App.js`、`contexts/`、`services/images.js` 等依赖文件
- 其 `src/services/ai.js` 调用的是旧版 API（`/api/ai?cmd=describe` 查询参数风格），
  与当前后端路由（`/api/ai/describe`）不匹配，运行时必然 404

可用前端请使用仓库根目录的 `frontend/`（功能为其超集：文件 + 图片 + AI 搜索 + AI 助手）。
待确认无历史参考价值后，可直接删除本目录。
