import colors from 'tailwindcss/colors'

/** @type {import('tailwindcss').Config} */
export default {
  // 扫描范围：Tailwind 只会为**在这里出现的完整类名**生成 CSS。
  // 动态拼接的类名（`bg-primary-${shade}`）扫不到，会静默丢样式。
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],

  theme: {
    extend: {
      colors: {
        // 主色 = Tailwind 内置的 sky 色系（50–950 全档，天蓝）。
        // 用 colors.sky 引用而不是抄一份十六进制：抄一份就多一处需要同步的地方，
        // 将来 Tailwind 调整调色板时也不会跟着走。
        // 用法：bg-primary-600 / text-primary-700 / border-primary-200 …
        primary: colors.sky,
      },
    },
  },

  plugins: [],
}
