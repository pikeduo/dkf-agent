<script setup lang="ts">
import type { CatalogSource, LinkEvidence, SchemaLink } from '../types/contest.ts'
const props = defineProps<{ links: SchemaLink[]; source?: CatalogSource }>()
/** 仅关联本次目录指纹一致的真实外键，绝不根据同名列猜连接。 */
function relation(evidence: LinkEvidence): string | null {
  if (!props.source || props.source.catalog_fingerprint !== evidence.catalog_fingerprint || evidence.kind !== 'foreign_key') return null
  const fk = props.source.foreign_keys.find(item => item.reference_id === evidence.reference_id)
  return fk ? fk.pairs.map(([left, right]) => `${fk.from_table}.${left} → ${fk.to_table}.${right}`).join('；') : null
}
</script>
<template>
  <section>
    <div class="panel-heading"><h3>Schema Linking 与关联依据</h3><span class="pill">{{ links.length }} 项</span></div>
    <p v-if="!links.length" class="empty">本次未返回 Schema 命中，未由前端补造。</p>
    <article v-for="(link, i) in links" :key="i" class="evidence-item">
      <strong>{{ link.mention }} · {{ link.state }}</strong>
      <div v-for="candidate in link.candidates" :key="candidate.candidate_id" class="schema-candidate">
        <p>{{ candidate.schema_name }}.{{ candidate.table }}<span v-if="candidate.column">.{{ candidate.column }}</span>
          <span class="pill">{{ candidate.candidate_id === link.selected_id ? '已选定' : '候选' }}</span> · 匹配分 {{ candidate.score ?? '未统计' }}（非概率）</p>
        <p v-for="(proof, n) in candidate.evidence" :key="n" class="identifier">
          {{ proof.kind }} · {{ proof.reference_id }}<br>目录指纹 {{ proof.catalog_fingerprint }}
          <strong v-if="relation(proof)" class="relation">{{ relation(proof) }}</strong>
        </p>
      </div>
    </article>
  </section>
</template>
