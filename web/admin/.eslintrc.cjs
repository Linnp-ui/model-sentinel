// 治本配置：no-undef 抓「build 全过、运行白屏」的未 import 标识符（已发生 3 次）；
// no-unused-vars 抓死 import / 定义了没挂载的组件（别名卡漏挂事故）。
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
  plugins: ['react', 'react-hooks'],
  extends: ['eslint:recommended', 'plugin:react/recommended'],
  settings: { react: { version: 'detect', runtime: 'automatic' } },
  rules: {
    'no-undef': 'error',
    'no-unused-vars': ['error', { args: 'none', varsIgnorePattern: '^_' }],
    'react/react-in-jsx-scope': 'off',
    'react/prop-types': 'off',
    'react-hooks/rules-of-hooks': 'error',
    'react-hooks/exhaustive-deps': 'off', // 存量代码依赖手动 disable-line，不翻旧账
  },
};
