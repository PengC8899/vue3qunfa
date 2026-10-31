import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/main.css'

import App from '@/App.vue'

// 沿用旧控制台的深色外观，Element Plus 自带 dark 变量
document.documentElement.classList.add('dark')

const app = createApp(App)
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}
app.use(createPinia())
app.use(ElementPlus, { locale: zhCn })
app.mount('#app')
