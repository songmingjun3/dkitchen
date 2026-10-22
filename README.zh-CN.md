<p align="center">
  <img src="https://raw.githubusercontent.com/songmingjun3/dkitchen/main/docs/assets/dkitchen.png" width="240" alt="两个像素风机器人厨师：大的那个摇服务铃，小的那个端着锅回应">
</p>

<h1 align="center">dkitchen</h1>

<p align="center">
  <a href="https://github.com/songmingjun3/dkitchen/blob/main/README.md">English</a> | <b>简体中文</b>
</p>

<p align="center">
  <b>你决定做什么菜，agent 替你管厨房。</b> 🧑‍🍳
</p>

<p align="center">
  <a href="https://github.com/songmingjun3/dkitchen/actions/workflows/tests.yml"><img src="https://github.com/songmingjun3/dkitchen/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/songmingjun3/dkitchen/blob/main/LICENSE"><img src="https://img.shields.io/github/license/917Dhj/dkitchen" alt="License"></a>
  <a href="https://x.com/dahuoji09"><img src="https://img.shields.io/badge/follow-%40dahuoji09-000000?logo=x" alt="X: @dahuoji09"></a>
</p>

dkitchen 是我每天用来跑科研仓库的多 agent 工作流。它给一组 **Deep​Seek​ Harness** 会话分配固定岗位，用 GitHub issue 作为它们交接工作的共同载体，写清什么时候必须停下来问你，并提供把这些串起来的工具。只有你能做的决定仍然留给你：研究什么、试什么、花多少。

它现在是一个 Deep​Seek​ Harness 插件。工具直接注册在 harness 进程里，所以装好这个 bundle 就装完了全部：不用装包，不用起常驻进程，也不用另外开一个 profile。

## 🍽️ 为什么要开一个厨房

AI agent 让执行变便宜了，瓶颈挪到了你身上。

一个 agent 时，每一步都要你盯着。几个 agent 时，你成了消息总线：在会话之间复制上下文、查任务有没有跑完、记住哪个会话负责哪件事、一天到晚批准鸡毛蒜皮。GPU 在两次运行之间空转，因为没人注意到上一次已经结束了。到晚上，你什么都碰过，又什么都没想。

米其林后厨解决的是同一个问题。主厨定菜单、尝菜；其他人各守一个岗，每张单子都是一张票，菜好了铃会响。主厨从不切洋葱，也从没跟丢过厨房。

dkitchen 就把你的 agent 摆成这个样子。你就是主厨。

## 🧑‍🍳 厨房里的人

<p align="center">
  <img src="https://raw.githubusercontent.com/songmingjun3/dkitchen/main/docs/assets/kitchen-zh.gif" width="100%" alt="像素风厨房：你和总设计谈定方案，核查核对票，总编排摇铃叫起空闲 GPU 上的执行者，审查并合并 PR，最后把需要你决定的一件事端到你面前">
</p>

| 岗位 | 在后厨里 | 做什么 | 跑在哪 |
| --- | --- | --- | --- |
| 👑 **你** | 主厨 | 定方向、批方案和预算、判断结果对你论文意味着什么。 | 你 |
| 🤖 **总编排** | 出菜口的副厨 | 排票、排 GPU、排并发。转达消息、审查并合并 PR、向你汇报事实。 | 一个 Deep​Seek​ Harness 会话，用你手上的最强模型 |
| 🤔 **总设计** | 研发菜谱的人 | 写票，设计方法和模型能看到的每一段文字，解读结果。 | 一个 Deep​Seek​ Harness 会话，用最强模型 |
| 🔍 **核查** | 试菜的人 | 在花钱之前对着代码和数据核对事实。只报告发现，不做决定。 | 一个 Deep​Seek​ Harness 会话，用便宜些的模型 |
| 🔪 **执行者** | 掌勺的 | 每张票一个全新会话，在自己的 worktree 里。实现、运行、在票上汇报。 | 每张票一个 Deep​Seek​ Harness 会话 |

**岗位由你选。** 不是每个仓库都需要四个岗位：`.dkitchen.toml` 里列出这套厨房实际跑的岗位，第一次初始化时由 skill 问你。去掉总设计，总编排就自己写票；去掉核查，总编排就自己做核对。以后想改是一次工具调用的事，而不是重写规则：

```text
dkitchen_config(action="set", key="kitchen.stations", value="orchestrator,executor,verifier")
```

每个岗位还有自己的模型路由，所以总编排可以跑你最强的模型，核查跑便宜的——同一个 harness、同一个 profile、同一套登录状态。

每个岗位都有明文规则，写清它负责什么、交接什么、什么时候必须停下。哪件事哪个岗位都不合适，agent 会来问你。

## 🎫 一张票怎么走完后厨

1. **你和总设计谈定要做什么。** 总设计把它写成一条 GitHub issue：目标、具体步骤、这项工作可以花的预算。
2. **核查对着代码和数据核对票的前提**：字段名、数据集范围、调用次数、成本估算。
3. **总编排把票派出去**，新开一个执行者会话，并挑空闲的 GPU。
4. **执行者干活，并在票上汇报。** 每条留言开头都是固定的四行：状态、在等谁、关键数字、下一步。长任务用 `dkitchen_launch` 起；任务退出时 harness 会把一条会话通知推给执行者，让它读输出继续。
5. **结果交给总设计**解读。总编排审查 PR、合并，并记录它做了什么、为什么。
6. **你读一份简短报告**，做下一个决定。

每个决定、每个请求、每个结果都落在票上，所以任何会话（或者一周后的你）都能从 GitHub 上接上这个故事的来龙去脉。

## ✋ 哪些决定留给你

规则里写清了属于主厨的决定：

- 研究方向、数据集怎么用、论文里主张什么；
- 超出一张票已批预算的模型运行、付费 API 调用、服务启动。

在这些事上，agent 只提案、然后等。只有你本人的话才算批准，总编排和总设计都不能替你批。

其余的事厨房自己搞定，就一条原则：**谁来做都会这么做，agent 就自己做；如果有几种合理做法，而这个选择会影响结果、花费或方向，就停下来等你。**

这就是你能走开的原因。你上课、睡觉、读论文的时候，`dkitchen_watch` 会在执行者跑完一个阶段、卡住、或者长时间没动静时唤醒总编排；你留言时也会；有新 PR 时也会。总编排只推进已经批准过的事，并给你留一份回来要做的决定清单。

## ⚖️ 一份资源池

科研仓库拥有的资源，比任何单个 agent 看得到的都多：几台服务器上的 GPU、一个共享模型服务、CPU 时间、API 预算、token，以及一天里的时间。在 dkitchen 里，跑着最强模型的总编排把这些当一份资源池来管。

- 🖥️ **算力。** 在空闲 GPU 之间排票，提高并发之前先看共享模型服务的负载。
- 💰 **钱。** 每张要跑模型的票带一份已批预算，管调用次数、成本和 GPU 小时。预算内的阶段立刻开工，超出的回到你这里。
- 🪙 **Token。** 一张票一个会话，上下文保持得小；核对工作交给更便宜的模型。
- ⏱️ **时间。** 任务退出时唤醒它的主人，没有事情在等谁想起来去看。

## 🧰 工具

### 🛎️ `dkitchen_*` 这批工具

装好 bundle，每个会话就拿到了厨房的命令。它们创建 harness 会话、在会话之间传消息、在长任务结束时唤醒发起它的会话。它们驱动的每个会话都是真正的 harness 会话：在 Deep​Seek​ Harness 窗口里看得见，由 harness 持久化，重启之后还在。

```text
dkitchen_new_session(name="消融", station="executor", issue=42, message="…")   # 为 #42 开一个会话
dkitchen_board()                                          # 这个仓库里谁在做什么
dkitchen_send(target="#42", sender="orchestrator", message="GPU 5 空出来了")
dkitchen_launch(name="train", command=["python", "train.py"])   # 后台运行，退出时唤醒我
dkitchen_logs(job="train: …")                             # 这个任务的最新输出
```

会话可以按 id、按名字、或者按 `#42` 这样的 issue 来寻址；dkitchen 会找到拥有那张票的会话。`dkitchen_board` 列出它在这个仓库里开过的每个会话，跟 harness 当前显示哪个会话无关。

工具就在 harness 里，所以会话不用开终端就能跑厨房，也不用在旁边维持任何进程。

### 📋 厨房的日常动作

各岗位每天的动作也是工具，所以每次都按同样稳妥的方式执行：

- `dkitchen_relay` 把消息发给某张票的执行者，只在确认送达之后才在票上留下对应留言。
- `dkitchen_merge` 只在被审查过的那个 HEAD 上合并 PR，且只在票上的执行者正在等总编排合并时。
- `dkitchen_watch` 报告一批总编排必须处理的 GitHub 动态，然后返回，好让它下一轮再调。
- `dkitchen_log` 维护一份总编排在你看不见时做了什么的日志。
- `dkitchen_check_refs` 核对核查引用的每个文件、每一行、每个摘要。

### 📜 后厨规矩

每个岗位遵守的规则在 [src/dkitchen-node/kitchen/](src/dkitchen-node/kitchen/README.md)：谁负责什么、票怎么写、什么时候停下发问，以及关于运行预算、模型提示词、登记产物、无人值守的可选模块。

## 🚀 上手

### 前置条件

- **Deep​Seek​ Harness**，以及你本来就能跑的 provider。插件需要 harness 提供 `tools`、`agents`、`sessions`、`jobs`、`approval` 这些服务——当前的版本都有。
- **一个 GitHub 仓库和已登录的 `gh` CLI**。只有票相关工具需要 `gh`，其余没有它也能用。
- 别的都不用。没有解释器要装：bundle 就是普通 JavaScript，由 harness 在自己的进程里运行。

### 1. 装 bundle

在 harness 里打开 **Plugins → Discover**，加上这个索引

```text
https://raw.githubusercontent.com/songmingjun3/dkitchen/main/docs/market/index.json
```

然后安装 `dkitchen`。或者直接把本地 checkout 装进去：**Plugins → install bundle**，指向 `src/dkitchen-node/`。harness 加载之后，新工具就会出现在每个会话里。

第一次启动还会把初始化 skill 装进这台机器级的 skill 目录，所以每个项目里都能用 `/setup-dkitchen`，不只是这个 checkout。

### 2. 配置你的厨房

在你的仓库里开一个 Deep​Seek​ Harness 会话，运行：

```text
/setup-dkitchen
```

这个 skill 会先读你的仓库，了解你在做什么、已经有什么：agent 说明文件、测试、GPU、模型调用、长任务。然后一个话题一个话题地问你：用什么语言、**要开哪些岗位**、**每个岗位用哪个模型**、哪些决定必须留给你、要哪些可选模块、有哪些算力。它会把打算写入的每一条规则、每个脚本和每项 GitHub 改动先拿给你看，你同意后才写。之后想改设置，随时再运行一次。

## ❓ 常见问题

**让 agent 无人值守地跑，安全吗？**
凡是会改变方向、结果或花费的事，它们都会停下；批准只认你在票里的原话。每个动作都是 GitHub 上的一条留言或一个 PR，第二天早上喝咖啡的时候就能把昨晚的事查一遍。

**适合我的仓库吗？**
这些规则是为计算类科研写的，其中大部分也适用于任何有长任务的仓库。安装 skill 会按你的仓库改写这些规则。

**Windows 上能用吗？**
能，而且没区别。旧的命令行工具在 Linux 上靠 tmux、在 Windows 上靠分离进程；插件两边都用 harness 自己的任务登记，所以没有平台相关的细节要记。

**要装好几个 agent 产品吗？**
不用。每个岗位都是一个 Deep​Seek​ Harness 会话，一个 harness 进程服务全部。不同岗位仍然可以用不同模型。

**需要 Python 吗？**
不需要。早期版本带一个 Python 命令行工具，通过 broker 和 MCP server 用 Agent Client Protocol 驱动会话。插件在进程内做同样的事，那套运行时已经删掉了。

**小项目用这么多流程，会不会太重？**
先只开总编排和执行者——`[kitchen] stations = ["orchestrator", "executor"]`——需要时再加总设计和核查。

## 💡 关于

*d* 是我的记号。dkitchen 是我管自己厨房的方式，写下来，好让你也能开起自己的厨房。它诞生在我的科研仓库里：这个仓库在最初的四周里，用这套工作流处理了 171 个 issue，合并了 111 个 PR。

米其林后厨这个比喻来自 Lauren Tan，网名 [Poteto](https://github.com/poteto)。这套工作流的很多做法都受到了 Poteto 管理 agent 方式的启发。用 GitHub issue 管票的做法，最初改自 [Matt Pocock 的 skills](https://github.com/mattpocock/skills) 里的 issue tracker 配置。谢谢两位。🙏

## 许可证

[MIT](LICENSE)
