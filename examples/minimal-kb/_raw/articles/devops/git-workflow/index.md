# Git 工作流笔记

一些零散的想法，还没整理完。

## rebase vs merge

rebase 保持线性历史，merge 保留分支结构。团队里有人喜欢 rebase，有人喜欢 merge，目前没有统一规范。

TODO：找时间整理一下两种方式的优劣对比。

## commit message

试过 conventional commits，感觉不错。但有时候懒得写 type，直接 `fix: xxx` 或 `update: xxx`。

## branch naming

看到过 `feature/xxx`、`bugfix/xxx`、`hotfix/xxx` 的规范，但我们团队目前没有强制。

## 待补充

- git bisect 的用法还没搞清楚
- submodule 和 subtree 的选择
- git worktree 的实际应用场景
