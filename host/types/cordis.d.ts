// tsconfig.json 的 paths 把 'cordis' 指向本文件，所以它会被 TypeScript 和 tsx 同时加载。
// 类型面交给 ./cordis-types.js 对应的声明文件，运行时面交给同名的 JS 文件。
export * from './cordis-types.js'
