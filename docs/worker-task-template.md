# 워커 태스크 지시문 템플릿

`CLAUDE.md`가 작업 규율을 담당한다. 지시문에는 **무엇을 만들지**와
**그 태스크 고유의 함정**만 쓴다. 규율을 다시 적지 않는다.

에이전트만 읽으므로 **영어로 쓴다.** 25~30줄을 넘기지 않는다.

```text
[GOAL] One sentence. Why this matters to the critical path.

[BUILD]
  path/to/module.py       what it does
  tests/test_module.py

[VERIFIED — already checked, do not re-verify]
  Paste exact API signatures, response field names, formulas, or schemas
  that were confirmed working. This is what keeps the worker off a
  wild goose chase and out of the design docs.

[TRAPS — specific to this task]
  Only non-obvious failure modes. Two or three at most.
  Skip anything already in CLAUDE.md.

[SCOPE]
  Touch only: <dirs>
  Other tracks own: <dirs>

[DONE]
  Anything beyond the standard definition of done in CLAUDE.md.
```

## 왜 이 형태인가

측정으로 확인한 것:

- W4는 검증된 API 파라미터를 지시문에 받아 **읽기 15회 / 중복 0KB**로 가장 깨끗했다
- W3는 그런 정보가 없어 **읽기 47회 / 중복 40KB**를 썼고 문서를 12조각으로 나눠 읽었다

워커가 문서를 뒤지게 두는 대신 답을 지시문에 넣으면 턴이 줄고, 턴이 줄면
컴팩션이 줄고, 컴팩션이 줄면 요약 래칫이 시작되지 않는다.
