---
name: classify_news_item
description: Same as assess_news.md but with stricter token budget for batch processing. Used by news_classifier.
---

You see a single news headline + body. Output ONLY a JSON object. No explanation.

```json
{"impact":"low|medium|high|critical","sentiment":-1.0..1.0,"symbols":["CL"],"category":"supply|demand|geopolitical|macro|technical","decay_hours":1|6|24|72,"actionable":true|false}
```

Stop. No prose. No surrounding text. Just the JSON.
