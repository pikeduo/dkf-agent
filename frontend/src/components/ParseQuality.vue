<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ quality: Record<string, unknown> | null }>()
// 只显示质量契约的枚举与数量，不整体展开 diagnostics/visual_assets 中的私有指针。
const fields = computed(() => {
  // 旧文档未返回质量时保留未知，不补 COMPLETE 或置信度。
  const q = props.quality
  if (!q) return null
  return {
    content: ['COMPLETE', 'PARTIAL'].includes(String(q.content)) ? String(q.content) : '未返回',
    structure: ['BASE', 'ENHANCED', 'DEGRADED'].includes(String(q.structure)) ? String(q.structure) : '未返回',
    evidence: ['VERIFIED', 'PARTIAL'].includes(String(q.evidence)) ? String(q.evidence) : '未返回',
    missing: Array.isArray(q.missing_pages) ? q.missing_pages.filter(p => Number.isInteger(p) && Number(p) > 0).join('、') || '无' : '未返回',
    visual: Array.isArray(q.visual_review_pages) ? q.visual_review_pages.filter(p => Number.isInteger(p) && Number(p) > 0).join('、') || '无' : '未返回',
    quarantined: typeof q.quarantined_count === 'number' ? q.quarantined_count : '未返回',
  }
})
</script>
<template>
  <div><h4>解析质量</h4><p v-if="!fields" class="hint">未返回质量指标，不代表内容完整。</p>
    <dl v-else class="metrics"><div><dt>内容</dt><dd>{{ fields.content }}</dd></div><div><dt>结构</dt><dd>{{ fields.structure }}</dd></div><div><dt>证据</dt><dd>{{ fields.evidence }}</dd></div><div><dt>隔离内容数</dt><dd>{{ fields.quarantined }}</dd></div><div><dt>缺页</dt><dd>{{ fields.missing }}</dd></div><div><dt>视觉待审页</dt><dd>{{ fields.visual }}</dd></div></dl>
  </div>
</template>
