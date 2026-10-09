# 首轮案例导航

**这是给你和评审看的导航，不要上传给被测模型。所有内容均为合成资料，尚未实际调用模型。**

基础题为每个场景的 01–04，共 12 题；岗位与招投标的 05/06 是四个附加配对变体。具体标准见 [验收标准](rubric.md)。

| 案例 | 场景 | 资料状态 | 输入文件 | 评审材料 |
|---|---|---|---|---|
| career-01 | 岗位匹配 | 资料充分 | [数据分析岗位说明](inputs/career-01/jd.md)、[申请人背景](inputs/career-01/background.md) | [评分与回答卡](grader/career-01.json) |
| career-02 | 岗位匹配 | 关键缺失 | [数据分析岗位说明](inputs/career-02/jd.md)、[申请人背景](inputs/career-02/background.md) | [评分与回答卡](grader/career-02.json) |
| career-03 | 岗位匹配 | 资料冲突 | [数据分析岗位说明](inputs/career-03/jd.md)、[申请人背景](inputs/career-03/background.md)、[申请人作品说明](inputs/career-03/portfolio.md) | [评分与回答卡](grader/career-03.json) |
| career-04 | 岗位匹配 | 长文关键条件 | [数据分析岗位说明](inputs/career-04/jd.md)、[申请人背景](inputs/career-04/background.md) | [评分与回答卡](grader/career-04.json) |
| career-05 | 岗位匹配 | 关键事实变化 | [数据分析岗位说明](inputs/career-05/jd.md)、[申请人背景](inputs/career-05/background.md) | [评分与回答卡](grader/career-05.json) |
| career-06 | 岗位匹配 | 无关内容变化 | [数据分析岗位说明](inputs/career-06/jd.md)、[申请人背景](inputs/career-06/background.md) | [评分与回答卡](grader/career-06.json) |
| tender-01 | 招投标研判 | 资料充分 | [栖屿项目说明](inputs/tender-01/requirements.md)、[虚构机构甲项目资料](inputs/tender-01/organization.md) | [评分与回答卡](grader/tender-01.json) |
| tender-02 | 招投标研判 | 关键缺失 | [石汀项目要求](inputs/tender-02/requirements.md)、[虚构机构乙资料](inputs/tender-02/organization.md) | [评分与回答卡](grader/tender-02.json) |
| tender-03 | 招投标研判 | 资料冲突 | [砾湾项目规则](inputs/tender-03/requirements.md)、[虚构机构丙方案记录甲](inputs/tender-03/organization.md)、[虚构机构丙方案记录乙](inputs/tender-03/organization-b.md) | [评分与回答卡](grader/tender-03.json) |
| tender-04 | 招投标研判 | 长文关键条件 | [榆灯完整项目文件](inputs/tender-04/requirements.md)、[虚构机构丁资料摘要](inputs/tender-04/organization.md) | [评分与回答卡](grader/tender-04.json) |
| tender-05 | 招投标研判 | 关键事实变化 | [栖屿项目说明](inputs/tender-05/requirements.md)、[虚构机构甲项目资料](inputs/tender-05/organization.md) | [评分与回答卡](grader/tender-05.json) |
| tender-06 | 招投标研判 | 无关内容变化 | [栖屿项目说明](inputs/tender-06/requirements.md)、[虚构机构甲项目资料](inputs/tender-06/organization.md) | [评分与回答卡](grader/tender-06.json) |
| course-01 | 课程选择 | 资料充分 | [课程说明](inputs/course-01/course.md)、[学习背景](inputs/course-01/background.md) | [评分与回答卡](grader/course-01.json) |
| course-02 | 课程选择 | 关键缺失 | [课程说明](inputs/course-02/course.md)、[学习背景](inputs/course-02/background.md) | [评分与回答卡](grader/course-02.json) |
| course-03 | 课程选择 | 资料冲突 | [课程说明](inputs/course-03/course.md)、[学习背景](inputs/course-03/background.md)、[时间安排记录](inputs/course-03/availability.md) | [评分与回答卡](grader/course-03.json) |
| course-04 | 课程选择 | 长文关键条件 | [课程说明](inputs/course-04/course.md)、[学习背景](inputs/course-04/background.md) | [评分与回答卡](grader/course-04.json) |

课程选择的四题为预留验证集，当前没有用于调试；开发者编写并看过材料，不能宣称独立盲测。

## 操作时的问题

- **career-01**（判断日期 2026-10-02）：请结合这个岗位的要求和我的背景，分析我目前与岗位的匹配情况，以及是否适合投递；请说明判断依据和还需要确认的信息。
- **career-02**（判断日期 2026-10-02）：请结合这个岗位的要求和我的背景，分析我目前与岗位的匹配情况，以及是否适合投递；请说明判断依据和还需要确认的信息。
- **career-03**（判断日期 2026-10-02）：请结合这个岗位的要求和我的背景，分析我目前与岗位的匹配情况，以及是否适合投递；请说明判断依据和还需要确认的信息。
- **career-04**（判断日期 2026-10-02）：请结合这个岗位的要求和我的背景，分析我目前与岗位的匹配情况，以及是否适合投递；请说明判断依据和还需要确认的信息。
- **tender-01**（判断日期 2026-10-02）：以2026-10-02为判断时点，结合项目说明和机构资料，分析我们目前是否具备参与栖屿项目的基础。请列出关键依据、不确定事项以及参与前需要处理的事项；缺少关键信息时再问我。
- **tender-02**（判断日期 2026-10-02）：以2026-10-02为判断时点，结合石汀项目要求和我们的资料，分析现在是否适合参与，指出需要补充什么，并说明已有依据。
- **tender-03**（判断日期 2026-10-02）：请结合砾湾项目规则与我们提交的两份记录，按2026-10-02的情况分析能否参与。把影响结论的依据和需要我确认的事项说清楚。
- **tender-04**（判断日期 2026-10-02）：按2026-10-02的情况，结合完整项目文件和机构摘要，判断我们是否适合按现有方案参与榆灯项目。请优先指出会改变参与判断的条件，并引用对应依据。
- **course-01**（判断日期 2026-10-02）：请结合我的学习背景与课程资料，判断课程内容、先修基础、时间和全部必缴费用是否适合我，说明依据、未知项及下一步建议。
- **course-02**（判断日期 2026-10-02）：请结合我的学习背景与课程资料，判断课程内容、先修基础、时间和全部必缴费用是否适合我，说明依据、未知项及下一步建议。
- **course-03**（判断日期 2026-10-02）：请结合我的学习背景与课程资料，判断课程内容、先修基础、时间和全部必缴费用是否适合我，说明依据、未知项及下一步建议。
- **course-04**（判断日期 2026-10-02）：请结合我的学习背景与课程资料，判断课程内容、先修基础、时间和全部必缴费用是否适合我，说明依据、未知项及下一步建议。

成对变体沿用基题的问题，仅使用对应变体的输入文件。不要把基题与变体全部同时绑定到一个任务。
