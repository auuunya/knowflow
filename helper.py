from textwrap import dedent


HELP_TEXT = dedent(
    """\
    ====================================================
    knowflow - LLM Knowledge Compiler
    ====================================================

    用法
      python3 knowflow.py build [stage] [--no-split]
      python3 knowflow.py lint
      python3 knowflow.py query <keyword>
      python3 knowflow.py research

    常用命令
      build          运行完整编译流程
      build private  仅重建 private wiki / index / private graph
      build public   基于现有 private 结果重建 public 内容
      lint           校验知识库，输出 blocking / warning
      query          本地检索，如 `query ai agent`
      research       生成 research 原始草稿，不发布

    build stages
      clean, index, split, topics, sources, compile,
      audit, graph, log

    示例
      python3 knowflow.py build
      python3 knowflow.py build private
      python3 knowflow.py build audit
      python3 knowflow.py lint
      python3 knowflow.py query ai agent
    """
)


def print_help():
    print(HELP_TEXT, end="")
