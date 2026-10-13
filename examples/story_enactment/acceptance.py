# Copyright 2026 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Acceptance assets for the manual review of spec section 13.2.

This module hosts the three fixed scenario configurations of appendix C
plus the human review checklist. Everything here is static content: no
language model is constructed or called, so the assets can be diffed and
versioned like any other part of an experiment protocol.

The three scenarios (each with 4-5 characters, at least one game-master-
only secret, and six or more whitelisted environment events):

1. 老宅遗嘱 — the default example (see ``story_config.make_example_config``).
2. 终面疑云 — a fraud-tainted final interview.
3. 婚礼上的旧歌 — a wedding shadowed by an old flame.
"""

from examples.story_enactment import story_config


def make_scenario_one_config() -> story_config.StoryConfig:
  """Returns the appendix-C scenario one: the old estate dispute.

  This reuses the package's default example configuration verbatim so
  the acceptance runs and the tutorial example stay in lockstep.

  Returns:
    The 老宅遗嘱 configuration (周律师 is the player character).
  """
  return story_config.make_example_config()


def make_scenario_two_config() -> story_config.StoryConfig:
  """Returns the appendix-C scenario two: a fraud-tainted final interview.

  Candidate 唐薇's résumé claims a flagship project that her old friend
  and former colleague 高翔 actually completed alone; only the two of
  them (and the game master) know. The interviewer 郑兰 (player
  character) holds unpublished background-check leads, while hiring
  manager 罗启铭 pushes to close the position today. The environment can
  make phones ring and popups appear, but only the player decides what
  郑兰 does with them.

  Returns:
    A valid five-character configuration.
  """
  return story_config.StoryConfig(
      title='终面疑云',
      location='乙公司总部十七层的小型面试间',
      interior=True,
      time_label='工作日上午',
      world_facts=(
          '乙公司是行业头部的大厂，今天在此为高级项目经理一职举行终面。',
          '面试间一面是玻璃墙，外面就是开放办公区，前台与电梯间人来人往。',
          '候选人唐薇已通过前三轮筛选，今天由人力、技术与用人部门共同终审。',
      ),
      gm_only_facts=(
          (
              '真相：唐薇简历中的"星辰云迁移项目"由高翔一人完成，'
              '她只整理过两周文档，两人对此心照不宣。'
          ),
          '真相：唐薇的项目管理专家证书购自代考团伙，编号在发证机构官网查无记录。',
          (
              '真相：昨晚那封匿名举报邮件来自甲公司一位被裁的旧同事，'
              '他并不知道高翔就在乙公司。'
          ),
      ),
      allowed_environment_events=(
          '前台转接进一通电话，自称是唐薇简历上的推荐人、甲公司前上司周女士',
          '方悦怀里的面试材料散落一地，背调摘要混在其中摊在了桌面上',
          '前台敲门递进一张便条：楼下有位先生自称候选人的旧同事，求见一面',
          '楼宇消防演习警报骤响，广播请大家沿疏散通道有序撤离',
          '郑兰的电脑弹出背调系统核查完成的弹窗',
          '一位不速之客敲开面试间的门，自称与唐薇在甲公司共事过三年',
      ),
      immutable_facts=(
          '今天确为乙公司高级项目经理职位的终面，唐薇是唯一候选人。',
          '高翔、郑兰、方悦、罗启铭都是乙公司在职员工，唐薇还不是。',
          '唐薇的简历上白纸黑字写着：曾在甲公司主导星辰云迁移项目。',
          '唐薇三年前确曾在甲公司任职，之后两年处于待业状态。',
      ),
      characters=(
          story_config.CharacterInfo(
              name='唐薇',
              identity='应聘者，竞聘乙公司高级项目经理职位，时隔两年的第一场终面',
              goal='拿下这份工作撑起家里的开销，同时不让简历里的水分被当场戳穿',
              hard_constraint='绝不主动承认简历中任何不实之处',
              secret=(
                  '星辰云迁移项目实为高翔一人完成，她只帮了两周文档；'
                  '那张项目管理专家证书是花钱买的，编号经不起查验。'
              ),
              public_facts=(
                  '十年产品岗位从业经历，其中三年任职于甲公司。',
                  '简历写着：曾在甲公司主导星辰云迁移项目，提前两个月交付。',
              ),
              style='表述流畅漂亮，惯用数字和行业黑话包装经历，被追问细节时爱笑着岔开话题',
              initial_relationship_notes=(
                  '与高翔是甲公司旧同事兼多年挚友，这层关系她刻意瞒着所有人；'
                  '对郑兰又敬又怕；暗暗祈祷推荐人别多嘴。'
              ),
          ),
          story_config.CharacterInfo(
              name='高翔',
              identity='乙公司技术部资深架构师，甲公司出身，今天作为用人部门代表列席终面',
              goal='帮公司把好技术关，又不能让唐薇的把柄毁掉她，更不能烧到自己',
              hard_constraint='绝不当众暴露自己与唐薇的旧识关系',
              secret=(
                  '简历里那个项目是他一个人熬夜做完的；'
                  '唐薇被裁后房贷和老人看病压身，他才默许她那样写。'
              ),
              public_facts=(
                  '乙公司技术部资深架构师，入职四年。',
                  '今天受邀列席终面，负责评估候选人的技术底子。',
              ),
              style='提问惜字如金，追问技术细节时格外锋利，偶尔用一句玩笑化解冷场',
              initial_relationship_notes=(
                  '与唐薇是挚友兼前同事，关系只有两人知道；'
                  '敬重郑兰的严谨；最担心旧同事不请自来。'
              ),
          ),
          story_config.CharacterInfo(
              name='郑兰',
              identity='乙公司人力资源总监，今天的终面主考官（玩家角色）',
              goal='在今天之内查清简历疑点，为公司把好最后一关，守住十年零看走眼的纪录',
              hard_constraint='绝不未经证实就给候选人下定论',
              secret=(
                  '昨晚她收到一封直指唐薇简历注水的匿名邮件；'
                  '官方渠道查询显示那张证书的编号并不存在。'
              ),
              public_facts=(
                  '主持乙公司招聘十年，以严谨著称。',
                  '今天的终面由她主持并最终签署录用意见。',
              ),
              style='提问环环相扣，先礼后兵，惯用"我们再回到您刚才说的那一点"',
              initial_relationship_notes=(
                  '一向信任高翔的专业判断；对唐薇第一印象不错却存疑；'
                  '对匿名邮件的可信度保持警觉。'
              ),
          ),
          story_config.CharacterInfo(
              name='方悦',
              identity='乙公司人事部招聘专员，负责今天终面的会务、材料与计时',
              goal='把流程安排得滴水不漏，别在自己这一环出任何岔子',
              hard_constraint='绝不外泄候选人的隐私材料',
              secret='两年前她自己也曾美化过简历才进了乙公司，因此暗暗同情唐薇。',
              public_facts=(
                  '入职乙公司两年，负责产品线招聘会务。',
                  '今天掌管签到、材料发放与面试计时。',
              ),
              style='语速快，爱列表式汇报，一紧张就反复确认"这样可以吗"',
              initial_relationship_notes='对郑兰又敬又怕；把高翔当自家兄长；对候选人保持职业性的友善。',
          ),
          story_config.CharacterInfo(
              name='罗启铭',
              identity='乙公司产品部总监，唐薇入职后的直接上司，专程赶来见候选人',
              goal='下季度的项目不能没人扛，今天必须拍板要人，宁可先进来再补短板',
              hard_constraint='绝不容许部门再出试用期翻车的事故',
              secret='他已向大老板承诺下周一让新项目经理到位，这个死线他没告诉任何人。',
              public_facts=(
                  '产品线负责人，用人需求最急的人。',
                  '今天行程排得很满，只留了半小时给终面。',
              ),
              style='大包大揽，爱表态爱拍板，口头禅是"先干起来再说"',
              initial_relationship_notes=(
                  '催人力资源部加急招聘已经一个月；'
                  '对高翔的技术眼光不完全买账；初见唐薇印象颇佳。'
              ),
          ),
      ),
      conflict_start=(
          '终面开场，郑兰请唐薇讲讲星辰云迁移项目里最难的一次决策；'
          '唐薇讲得漂亮，高翔却放下笔追问了一个只有真正主导者才答得上的数据口径，'
          '罗启铭笑着打圆场，面试间里的空气却凝住了。'
      ),
      allowed_endings=(
          '真相大白：证书与项目双双败露，唐薇当场承认造假，高翔的沉默也被摆上台面。',
          '瞒天过海：疑点被圆了过去，唐薇拿到录用通知，高翔在心里埋下一根刺。',
          '主动坦白：唐薇在通知发出前找到郑兰和盘托出，争取以降级录用重新开始。',
          '僵局悬置：面试因突发中断未有结论，是否录用交由更高层裁决。',
      ),
  )


def make_scenario_three_config() -> story_config.StoryConfig:
  """Returns the appendix-C scenario three: a wedding shadowed by an old flame.

  The bridesmaid 沈玉 and the groom 陆屿 were a couple five years ago;
  only the two of them (and the game master) know. The bride's brother
  苏航 (player character) caught a fragment of a late-night exchange
  during the rehearsal and intends to find out the truth before his
  sister 苏晴 walks down the aisle, without wrecking the wedding on a
  hunch. The environment supplies champagne towers, wrong photos, old
  songs and a looming storm; only the player decides what 苏航 asks and
  when.

  Returns:
    A valid five-character configuration.
  """
  return story_config.StoryConfig(
      title='婚礼上的旧歌',
      location='城郊园林酒店的婚礼宴会厅',
      interior=True,
      time_label='初夏傍晚',
      world_facts=(
          '今天是苏晴与陆屿的婚礼，傍晚的仪式之后是晚宴。',
          '宴会厅一侧立着投影幕，即将播放两位新人的成长影像。',
          '沈玉是新娘苏晴的大学室友，专程飞来担任伴娘。',
      ),
      gm_only_facts=(
          (
              '真相：沈玉与陆屿五年前曾相恋近一年，和平分手后约定永不再提，'
              '世上只有他们二人知情。'
          ),
          '真相：婚礼影像的素材盘是沈玉帮忙导入的，里面混进了一张她与陆屿当年的合影。',
          '真相：司仪歌单里那首老歌正是两人当年的定情曲，点歌的宾客并不知情。',
      ),
      allowed_environment_events=(
          '花童跑动中撞倒了香槟塔，杯塔哗啦倾倒了一地',
          '司仪误切素材，投影幕上闪过一张不在流程单上的旧照片',
          '音响里突然响起一首九十年代的老歌，宾客纷纷侧耳',
          '一桌迟到宾客推门入席，打断了正在进行的致辞',
          '花童把戒指枕递错了人，径直送到了伴娘手里',
          '天色骤暗，礼宾员进来催促把户外环节挪进室内，暴雨将至',
      ),
      immutable_facts=(
          '今天是苏晴与陆屿的婚礼，仪式与晚宴都在这座宴会厅举行。',
          '苏航是新娘苏晴的亲弟弟，沈玉是新娘的伴娘。',
          '陆屿与苏晴相恋两年后订婚，婚期筹备了近一年。',
          '金叔是今天受雇主持婚礼的司仪，不参与两家私事。',
      ),
      characters=(
          story_config.CharacterInfo(
              name='沈玉',
              identity='新娘苏晴的大学室友兼伴娘，绘本作者，专程飞来参加婚礼',
              goal='体面地完成伴娘职责，把五年前的事留在昨天，护住苏晴的大日子',
              hard_constraint='绝不主动提起与新郎的过去',
              secret=(
                  '她与陆屿五年前相恋近一年，分手时约定永不再提；'
                  '行李箱夹层里还压着两人当年的电影票根。'
              ),
              public_facts=(
                  '苏晴的大学室友，相识八年，为人细心妥帖。',
                  '自由职业绘本作者，作品小有名气。',
              ),
              style='语气轻柔得体，笑容标准，说到"以前"之类的词会突然放慢半拍',
              initial_relationship_notes=(
                  '对苏晴有姐妹般的情谊，也藏着愧疚；'
                  '面对陆屿极力维持礼貌的距离；隐约察觉苏航在观察自己。'
              ),
          ),
          story_config.CharacterInfo(
              name='陆屿',
              identity='新郎，建筑事务所合伙人，今天婚礼的主角之一',
              goal='顺顺利利走完仪式，给苏晴一个无可挑剔的婚礼',
              hard_constraint='绝不让任何旧事在今天惊扰新娘',
              secret=(
                  '他与沈玉五年前的恋情和那句"三十岁再见"；'
                  '誓词里"第一次心动"写的其实是那年冬天。'
              ),
              public_facts=(
                  '建筑事务所合伙人，与苏晴相恋两年订婚。',
                  '大学毕业后一直定居本市，人缘极好。',
              ),
              style='举止周到，笑意温和，情绪起伏时会下意识转动腕上的手表',
              initial_relationship_notes='深爱苏晴，也珍视眼下的生活；对沈玉怀着旧愧；对苏航的追问保持警觉。',
          ),
          story_config.CharacterInfo(
              name='苏晴',
              identity='新娘，市图书馆策展人，今天婚礼的女主角',
              goal='让筹备了一年的婚礼完美落幕，不让任何小插曲留下遗憾',
              hard_constraint='绝不在宾客面前失态',
              secret='她早就注意到陆屿手机里存着一张并非两人的旧合照，只是一直没敢问。',
              public_facts=(
                  '市图书馆策展人，与陆屿相恋两年。',
                  '婚礼的每个细节都由她亲手敲定。',
              ),
              style='说话爽利明亮，带着总控台气质，遇到意外先看司仪再微笑',
              initial_relationship_notes=(
                  '视沈玉为最重要的闺蜜；对弟弟的过度保护又好气又好笑；'
                  '对陆屿全然信任，只在极少的瞬间有一丝说不清的迟疑。'
              ),
          ),
          story_config.CharacterInfo(
              name='苏航',
              identity='新娘苏晴的亲弟弟，婚礼的编外总管（玩家角色）',
              goal='查清彩排夜撞见的那一幕，别让姐姐蒙在鼓里走进婚礼',
              hard_constraint='绝不无凭无据当众闹场让姐姐难堪',
              secret=(
                  '昨晚彩排散场，他在露台撞见沈玉和陆屿低声争执，'
                  '听到一句"你真的想清楚了吗"；他没有告诉任何人。'
              ),
              public_facts=(
                  '苏晴的亲弟弟，研究生刚毕业。',
                  '婚礼上主动揽下调度迎宾的杂务。',
              ),
              style='说话直来直去，问题一个接一个，护姐心切时嗓门会变大',
              initial_relationship_notes=(
                  '对准姐夫表面客气、心里存疑；'
                  '从小把沈玉当半个姐姐，如今却第一个怀疑她；只盼司仪按流程走。'
              ),
          ),
          story_config.CharacterInfo(
              name='金叔',
              identity='从业二十年的婚礼司仪，今天全场的流程总控',
              goal='把每个环节掐在点上，任何突发都圆成"惊喜"',
              hard_constraint='绝不拿新人的私事当包袱抖',
              secret='他手里的歌单混进了一首不在流程单上的老歌，到现在没核对出是谁点的。',
              public_facts=(
                  '从业二十年，主持过上千场婚礼。',
                  '今天的流程、音响、投影都归他调度。',
              ),
              style='嗓音洪亮，串场词张口就来，惯用"来，掌声送给他们"',
              initial_relationship_notes='见惯婚礼上的暗流，只求流程无恙；把苏航当成热心的自家晚辈。',
          ),
      ),
      conflict_start=(
          '晚宴将至，司仪请伴娘沈玉上台为新人致祝酒辞；'
          '她刚举起酒杯，苏航忽然从席间站起来，笑着一字一顿地问：'
          '"我想先请教伴娘——你和我姐夫，到底是什么时候认识的？"'
          '满堂笑语一下子静了，陆屿转杯子的手停在了半空。'
      ),
      allowed_endings=(
          '旧情坦白：陆屿或沈玉在仪式前向苏晴和盘托出，婚礼在震动后由新娘重新做出选择。',
          '体面告别：两人私下郑重道别，秘密守住，婚礼如期完成，只有彼此知道代价。',
          '当众揭穿：苏航拿到实证当众摊牌，婚礼中断，两个家庭开始艰难的清算。',
          '疑云消散：兄弟的疑问被合理解释化解，婚礼完成，疑点只留在个别人心里。',
      ),
  )


# Review dimensions of the appendix-C checklist: (dimension, question,
# how to fill the record column).
_REVIEW_ROWS = (
    ('角色区分', '台词是否能盲猜出说话者？', 'turn_id + 评语'),
    ('因果连续', '事件是否被后续引用而非遗忘？', '同上'),
    ('信息泄漏', '秘密是否在角色不知情时泄露？', '同上'),
    ('对白重复', '相邻轮次是否复读？', '同上'),
    ('导演越权', 'resolved_event 是否替玩家角色行动？', '同上'),
    ('结尾状态', '终止原因是否与剧情吻合？', 'reason + 评语'),
)


def review_checklist_text(scenario_title: str) -> str:
  """Builds the human review checklist text for one scenario.

  The checklist mirrors appendix C: each run of a scenario is reviewed
  per dimension, every finding is annotated with the ``turn_id`` it
  concerns, and the pass thresholds are fixed by the user before
  release (beat progression and tension-in-range never substitute for
  these checks).

  Args:
    scenario_title: the reviewed scenario's title.

  Returns:
    The Markdown checklist, newline-terminated.
  """
  lines = [
      f'# 人工评审表 — {scenario_title}',
      '',
      '每场景跑 3 次，逐项标 turn_id；发布前由用户定通过阈值。',
      '',
      '| 维度 | 检查问题 | 记录 |',
      '|---|---|---|',
  ]
  for dimension, question, record in _REVIEW_ROWS:
    lines.append(f'| {dimension} | {question} | {record} |')
  return '\n'.join(lines) + '\n'


def print_review_checklist(scenario_title: str) -> None:
  """Prints the human review checklist for one scenario.

  Args:
    scenario_title: the reviewed scenario's title.
  """
  print(review_checklist_text(scenario_title), end='')
