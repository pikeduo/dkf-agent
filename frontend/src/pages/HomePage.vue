<script setup lang="ts">
defineEmits<{ navigate: [page: string] }>()
</script>
<template>
  <div class="page stack"><header class="page-header"><div><div class="eyebrow">CHINA ELECTRONICS CUP / TASK 08</div><h1 class="page-title">数知融问 · 可解释工作台</h1><p class="page-subtitle">从自然语言问题到真实 SQL、文档证据与可追溯的工具记录。</p></div></header>
    <section class="card card-pad"><h2>双服务，同源交互</h2><p>问数由独立竞赛服务处理；知识检索、上传、解析、向量化、RAG 与引用继续由原知识服务提供。</p><p class="notice warn">代码已整合，阶段 2 自动测试与真实服务联调、阶段 3 前端测试与浏览器联调仍待人工验收。阶段 1 Office 回归及真实 GPU Reindex 仍待验收。</p></section>
    <div class="feature-grid">
      <button class="card card-pad feature-button" @click="$emit('navigate', 'chat')"><span class="eyebrow">01 / SQL</span><h3>问数与主动澄清</h3><p>选择真实来源，绑定会话和条件，明确授权后执行。</p></button>
      <button class="card card-pad feature-button" @click="$emit('navigate', 'knowledge')"><span class="eyebrow">02 / EVIDENCE</span><h3>文档知识问答</h3><p>原文、真实页码、章节和回答引用。</p></button>
      <button class="card card-pad feature-button" @click="$emit('navigate', 'library')"><span class="eyebrow">03 / LIBRARY</span><h3>知识库管理</h3><p>多文件上传、异步状态、数量与解析质量。</p></button>
      <button class="card card-pad feature-button" @click="$emit('navigate', 'fusion')"><span class="eyebrow">04 / MANUAL LINK</span><h3>人工跨源联动</h3><p>确认 SQL 实体，再请求文档证据。</p></button>
    </div>
    <section class="card card-pad"><h3>尚未接入的能力</h3><p>统一 Agent HTTP、自动跨源规划、公式 HTTP、引用来源重新核验 HTTP、多租户用户系统均未实现。不会显示模拟回答、假健康状态、假耗时或成功示例。</p><p class="hint">上方健康检查只反映两个 API 进程存活，不代表数据库、模型或查询链路通过验收。</p></section>
  </div>
</template>
