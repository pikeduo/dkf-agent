<script setup lang="ts">
import type { Citation, Evidence } from '../types/knowledge.ts'
import { pageLabel } from '../utils/format.ts'
defineProps<{ items: (Evidence | Citation)[]; title?: string }>()
</script>
<template>
  <section class="evidence-panel">
    <div class="panel-heading"><h3>{{ title || '文档证据与引用' }}</h3><span class="pill">{{ items.length }} 条</span></div>
    <div v-if="items.length" class="evidence-list">
      <article v-for="(source, i) in items" :key="source.chunk_id" class="evidence-item">
        <div class="evidence-head"><strong><span class="source-n">{{ 'citation_id' in source ? `[${source.citation_id}]` : `证据 ${i + 1}` }}</span> {{ source.file_name }}</strong><span>{{ pageLabel(source) }}</span></div>
        <p class="hint">章节：{{ source.section ?? '未标注' }}</p><p class="evidence-text">{{ source.text }}</p>
        <p class="identifier">文档 {{ source.doc_id }} · Chunk {{ source.chunk_id }}</p>
        <div class="hint">检索分：{{ source.retrieval_score ?? '未统计' }} · 融合分：{{ source.fusion_score ?? '未统计' }} · 重排分：{{ source.rerank_score ?? '未统计' }}（均非概率）</div>
      </article>
    </div>
    <p v-else class="empty">当前没有原文证据。</p>
    <p class="hint">以上为后端本次返回的引用快照。来源重新核验 HTTP 尚未接入，页面不伪造额外验真状态。</p>
  </section>
</template>
