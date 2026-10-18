<script setup lang="ts">
import { onMounted } from 'vue'
import { knowledgeApi } from '../state/workbench.ts'
import { createKnowledgeWorkspace } from '../state/knowledge.ts'
import KnowledgeScope from '../components/KnowledgeScope.vue'
import KnowledgeAnswer from '../components/KnowledgeAnswer.vue'
const model = createKnowledgeWorkspace(knowledgeApi)
const s = model.state
// 进入页面仅读取本地管理目录，不加载或请求问答模型。
onMounted(() => { void model.bases() })
</script>
<template>
  <div class="page stack">
    <header class="page-header"><div><div class="eyebrow">KNOWLEDGE / CITATION</div><h1 class="page-title">文档知识问答</h1><p class="page-subtitle">复用原 Hybrid、重排与 RAG。检索证据和生成答案分开展示。</p></div></header>
    <section class="card card-pad stack"><KnowledgeScope :model="model" />
      <label for="knowledge-question">文档问题</label><textarea id="knowledge-question" v-model="s.question" class="textarea" rows="4" maxlength="4000" :disabled="s.busy" @input="model.invalidate"></textarea>
      <label>融合候选上限 top_k（1～100）<input v-model.number="s.topK" class="input" type="number" min="1" max="100" :disabled="s.busy" @input="model.invalidate"></label>
      <label class="check"><input v-model="s.allowCloud" type="checkbox" :disabled="s.busy">仅本次允许问题及最终证据正文外传给知识服务所配置的模型，并接受费用</label>
      <p class="hint">默认不允许。后端校验许可；不勾选生成按钮仍可提交 allow_cloud=false 并显示真实拒绝。只检索使用本地模型。</p>
      <div class="toolbar"><button class="btn outline" :disabled="s.busy || !s.docsLoaded || !s.question.trim()" @click="model.run('search')">仅检索原文证据</button><button class="btn primary" :disabled="s.busy || !s.docsLoaded || !s.question.trim()" @click="model.run('query')">{{ s.busy ? '请求处理中…' : '请求带引用的回答' }}</button></div>
    </section>
    <p v-if="s.error" class="notice danger" role="alert">{{ s.error }}</p>
    <KnowledgeAnswer :search="s.search" :answer="s.answer" :question="s.submittedQuestion" />
    <p class="hint">RAG 尚无统一 Trace HTTP 适配，不能将知识服务步骤填入竞赛 Trace。以上引用不能替代人工语义验收。</p>
  </div>
</template>
