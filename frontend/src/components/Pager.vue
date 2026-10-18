<script setup lang="ts">
defineProps<{ total: number; limit: number; offset: number; busy?: boolean }>()
defineEmits<{ change: [offset: number] }>()
</script>
<template>
  <div class="pager">
    <button class="btn small outline" :disabled="busy || offset === 0" @click="$emit('change', Math.max(0, offset - limit))">上一页</button>
    <span>{{ total ? offset + 1 : 0 }}–{{ Math.min(offset + limit, total) }} / {{ total }}</span>
    <button class="btn small outline" :disabled="busy || offset + limit >= total" @click="$emit('change', offset + limit)">下一页</button>
  </div>
</template>
