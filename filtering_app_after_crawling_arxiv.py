import asyncio
import gradio as gr
import json
import os
from openai import AsyncOpenAI
import aiofiles
from pathlib import Path
import glob
import time

# 默认配置
DEFAULT_CONFIG = {
    "api_key": "YOUR_API_KEY_HERE",
    "base_url": "https://api.openai.com/v1/",
    "model": "gpt-4-turbo",
    "rounds": 3,
    "max_concurrent": 50
}

# 预设提示词
COARSE_SYSTEM_PROMPT = """
Determine if this paper title is related to emotional support, psychological counseling, or multi-turn dialogue. Return True if there is any relevant content, otherwise return False.
<True/False>
"""

FINE_SYSTEM_PROMPT = """
Please carefully read the title and abstract of the paper and determine whether the paper is closely related to any of the following topics:
- Emotional support
- Psychological counseling
- Multi-turn dialogue
- Dialogue systems

Please conduct an in-depth analysis based on the content of the abstract. Return "True" only if the core content of the paper is indeed related to the above topics.
If the paper only mentions relevant concepts slightly or mainly focuses on other fields, return "False".

<True/False>
"""

def load_config():
    """加载配置文件"""
    config_path = "config.json"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            if "rounds" not in config:
                config["rounds"] = DEFAULT_CONFIG["rounds"]
            if "max_concurrent" not in config:
                config["max_concurrent"] = DEFAULT_CONFIG["max_concurrent"]
            return config
    else:
        # 创建默认配置文件
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return DEFAULT_CONFIG

def save_config(api_key, base_url, model, rounds, max_concurrent):
    """保存配置文件"""
    config = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "rounds": int(rounds),
        "max_concurrent": int(max_concurrent)
    }
    with open("config.json", 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return "配置已保存！"

def get_json_files():
    """获取主目录和 arxiv_papers_new 子目录下的所有JSON文件，并应用过滤规则"""
    # 获取主目录的文件
    main_dir_files = glob.glob("*.json")
    
    # 获取子目录的文件
    sub_dir_path = "arxiv_papers_new"
    sub_dir_files = []
    if os.path.isdir(sub_dir_path):
        sub_dir_files = glob.glob(os.path.join(sub_dir_path, "*.json"))

    # 合并两个列表
    all_files = main_dir_files + sub_dir_files

    # 定义过滤规则
    excluded_suffixes = ['_coarse_', '_fine_']
    excluded_filenames = ['config.json']
    subdir_excluded_filenames = ['last_crawl_time.json', 'failed_intervals.json']

    # 应用过滤
    filtered_list = []
    for file_path in all_files:
        base_name = os.path.basename(file_path)
        
        # 规则1: 过滤掉中间结果文件
        if any(suffix in base_name for suffix in excluded_suffixes):
            continue
        
        # 规则2: 过滤掉配置文件
        if base_name in excluded_filenames:
            continue
            
        # 规则3: 过滤掉子目录下的特定文件
        # is_in_subdir = os.path.dirname(file_path) == sub_dir_path
        is_in_subdir = sub_dir_path in file_path
        if is_in_subdir and base_name in subdir_excluded_filenames:
            continue
        
        filtered_list.append(file_path)
        
    return filtered_list

def get_result_files():
    """获取当前目录下的粗筛结果文件"""
    return [f for f in glob.glob("*.json") if 'coarse_final' in f]


def get_filename_with_suffix(original_filename, suffix):
    """在文件名的.json之前添加后缀，输出文件保存到当前目录"""
    base_filename = os.path.basename(original_filename)
    
    if base_filename.endswith('.json'):
        base_name = base_filename[:-5]  # 去掉.json
        return f"{base_name}_{suffix}.json"
    else:
        return f"{base_filename}_{suffix}"

async def check_paper_relevance_with_retry(client, paper_data, system_prompt, max_retries=3):
    """检查单个论文的相关性（粗筛）- 带重试机制"""
    for attempt in range(max_retries):
        try:
            # 兼容性修改：安全地获取和处理标题，以兼容新旧两种JSON格式
            title_text = paper_data.get('title', '').strip()
            # 保留split逻辑以兼容旧格式，同时对新格式也安全
            clean_title = title_text.split('author')[0].strip()
            user_content = f"论文标题: {clean_title}"

            response = await client.chat.completions.create(
                model=client.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]
            )
            result = response.choices[0].message.content.strip()
            return paper_data, "True" in result
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                print("等待60秒后重试...")
                await asyncio.sleep(60)
            else:
                print(f"处理标题 '{paper_data.get('title', 'N/A')}' 时出错 (已重试{max_retries}次): {e}")
                return paper_data, False

async def check_paper_relevance_detailed_with_retry(client, paper_data, system_prompt, max_retries=3):
    """基于标题和摘要检查单个论文的相关性（精排）- 带重试机制"""
    for attempt in range(max_retries):
        try:
            # 兼容性修改：安全地获取和处理标题与摘要
            title_text = paper_data.get('title', '').strip()
            abstract_text = paper_data.get('abstract', '').strip()
            # 保留split逻辑以兼容旧格式
            clean_title = title_text.split('author')[0].strip()
            content = f"论文标题: {clean_title}\n\n论文摘要: {abstract_text}"
            
            response = await client.chat.completions.create(
                model=client.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content}
                ]
            )
            result = response.choices[0].message.content.strip()
            return paper_data, "True" in result
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"API调用失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                print("等待60秒后重试...")
                await asyncio.sleep(60)
            else:
                print(f"处理论文 '{paper_data.get('title', 'N/A')}' 时出错 (已重试{max_retries}次): {e}")
                return paper_data, False

async def process_papers_single_round(client, papers_data, system_prompt, round_num, max_concurrent, is_fine=False, progress_callback=None):
    """单轮处理所有论文"""
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def limited_check(paper_data):
        async with semaphore:
            if is_fine:
                return await check_paper_relevance_detailed_with_retry(client, paper_data, system_prompt)
            else:
                return await check_paper_relevance_with_retry(client, paper_data, system_prompt)
    
    mode = "精排" if is_fine else "粗筛"
    print(f"开始第 {round_num} 轮{mode}检查 {len(papers_data)} 篇论文的相关性...")
    
    tasks = [limited_check(paper) for paper in papers_data if paper.get('title')]
    
    results = []
    completed = 0
    
    for completed_task in asyncio.as_completed(tasks):
        result = await completed_task
        results.append(result)
        completed += 1
        
        if progress_callback and len(tasks) > 0:
            progress = completed / len(tasks)
            progress_callback(progress, f"第{round_num}轮{mode}: {completed}/{len(tasks)}")
    
    relevant_papers = [paper_data for paper_data, is_relevant in results if is_relevant]
    
    print(f"第 {round_num} 轮{mode}找到 {len(relevant_papers)} 篇相关论文")
    
    return relevant_papers

async def coarse_screening(main_json_file, findings_json_file, system_prompt, config, progress_callback=None):
    """粗筛处理"""
    papers_data = []
    
    if not os.path.exists(main_json_file):
        return f"错误：文件 {main_json_file} 不存在"
    
    try:
        async with aiofiles.open(main_json_file, 'r', encoding='utf-8') as f:
            content = await f.read()
            main_data = json.loads(content)
            if 'papers' in main_data:
                papers_data.extend(main_data['papers'])
                print(f"读取主会议论文: {len(main_data['papers'])} 篇")
    except Exception as e:
        return f"读取或解析主文件 {main_json_file} 失败: {e}"

    if findings_json_file and os.path.exists(findings_json_file):
        try:
            async with aiofiles.open(findings_json_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                findings_data = json.loads(content)
                if 'papers' in findings_data:
                    papers_data.extend(findings_data['papers'])
                    print(f"读取Findings论文: {len(findings_data['papers'])} 篇")
        except Exception as e:
            return f"读取或解析Findings文件 {findings_json_file} 失败: {e}"

    
    print(f"总共需要处理: {len(papers_data)} 篇论文")
    
    client = AsyncOpenAI(
        api_key=config["api_key"], 
        base_url=config["base_url"],
        timeout=60.0
    )
    client.model = config["model"]
    
    all_rounds_results = []
    rounds = config.get("rounds", 3)
    max_concurrent = config.get("max_concurrent", 50)
    
    for round_num in range(1, rounds + 1):
        if progress_callback:
            progress_callback(0, f"开始第{round_num}轮粗筛...")
        
        relevant_papers = await process_papers_single_round(
            client, papers_data, system_prompt, round_num, max_concurrent, False, progress_callback
        )
        all_rounds_results.append(relevant_papers)
        
        round_data = {
            "round": round_num,
            "total_papers": len(papers_data),
            "relevant_papers_count": len(relevant_papers),
            "relevant_papers": relevant_papers
        }
        
        output_file = get_filename_with_suffix(main_json_file, f'coarse_round_{round_num}')
        async with aiofiles.open(output_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(round_data, ensure_ascii=False, indent=2))
        
        print(f"第 {round_num} 轮结果已保存到 {output_file}")
    
    all_relevant_papers = {}
    for round_papers in all_rounds_results:
        for paper in round_papers:
            # 使用标题作为键来去重
            if paper.get('title'):
                all_relevant_papers[paper['title']] = paper
    
    final_relevant_papers = list(all_relevant_papers.values())
    
    final_data = {
        "total_papers": len(papers_data),
        "rounds_count": rounds,
        "max_concurrent": max_concurrent,
        "round_results": [
            {
                "round": i,
                "count": len(round_papers)
            }
            for i, round_papers in enumerate(all_rounds_results, 1)
        ],
        "final_relevant_papers_count": len(final_relevant_papers),
        "relevant_papers": sorted(final_relevant_papers, key=lambda x: x.get('title', ''))
    }
    
    output_file = get_filename_with_suffix(main_json_file, 'coarse_final')
    async with aiofiles.open(output_file, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(final_data, ensure_ascii=False, indent=2))
    
    round_stats = "\n".join([f"- 第{i}轮筛选：{len(round_papers)} 篇" for i, round_papers in enumerate(all_rounds_results, 1)])
    
    result_text = f"""
粗筛完成！

处理统计：
- 总论文数：{len(papers_data)}
- 处理轮数：{rounds} 轮
- 最大并发数：{max_concurrent}
{round_stats}
- 最终结果：{len(final_relevant_papers)} 篇

结果已保存到：{output_file}
"""
    
    return result_text

async def fine_screening(input_json_file, system_prompt, config, progress_callback=None):
    """精排处理"""
    if not os.path.exists(input_json_file):
        return f"错误：文件 {input_json_file} 不存在"
    
    try:
        async with aiofiles.open(input_json_file, 'r', encoding='utf-8') as f:
            content = await f.read()
            coarse_data = json.loads(content)
            papers_data = coarse_data.get('relevant_papers', [])
            print(f"读取粗排结果: {len(papers_data)} 篇论文")
    except Exception as e:
        return f"读取或解析文件 {input_json_file} 失败: {e}"

    client = AsyncOpenAI(
        api_key=config["api_key"], 
        base_url=config["base_url"],
        timeout=60.0
    )
    client.model = config["model"]
    
    all_rounds_results = []
    rounds = config.get("rounds", 3)
    max_concurrent = min(config.get("max_concurrent", 50), 30)
    
    for round_num in range(1, rounds + 1):
        if progress_callback:
            progress_callback(0, f"开始第{round_num}轮精排...")
        
        relevant_papers = await process_papers_single_round(
            client, papers_data, system_prompt, round_num, max_concurrent, True, progress_callback
        )
        all_rounds_results.append(relevant_papers)
        
        round_data = {
            "round": round_num,
            "type": "fine_ranking",
            "input_papers": len(papers_data),
            "relevant_papers_count": len(relevant_papers),
            "relevant_papers": relevant_papers
        }
        
        output_file = get_filename_with_suffix(input_json_file, f'fine_round_{round_num}')
        async with aiofiles.open(output_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(round_data, ensure_ascii=False, indent=2))
        
        print(f"第 {round_num} 轮精排结果已保存到 {output_file}")
    
    all_relevant_papers = {}
    for round_papers in all_rounds_results:
        for paper in round_papers:
            if paper.get('title'):
                all_relevant_papers[paper['title']] = paper
    
    final_relevant_papers = list(all_relevant_papers.values())
    
    final_data = {
        "type": "fine_ranking_final",
        "input_papers": len(papers_data),
        "rounds_count": rounds,
        "max_concurrent": max_concurrent,
        "round_results": [
            {
                "round": i,
                "count": len(round_papers)
            }
            for i, round_papers in enumerate(all_rounds_results, 1)
        ],
        "final_relevant_papers_count": len(final_relevant_papers),
        "selection_rate": f"{len(final_relevant_papers)/len(papers_data)*100:.1f}%" if len(papers_data) > 0 else "0.0%",
        "relevant_papers": sorted(final_relevant_papers, key=lambda x: x.get('title', ''))
    }
    
    output_file = get_filename_with_suffix(input_json_file, 'fine_final')
    async with aiofiles.open(output_file, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(final_data, ensure_ascii=False, indent=2))
    
    round_stats = "\n".join([f"- 第{i}轮精排：{len(round_papers)} 篇" for i, round_papers in enumerate(all_rounds_results, 1)])
    
    result_text = f"""
精排完成！

处理统计：
- 输入论文数：{len(papers_data)}
- 处理轮数：{rounds} 轮
- 最大并发数：{max_concurrent}
{round_stats}
- 最终结果：{len(final_relevant_papers)} 篇
- 精排率：{final_data['selection_rate']}

结果已保存到：{output_file}
"""
    
    return result_text

def get_file_path(dropdown_value, upload_file):
    """获取文件路径，优先使用上传的文件"""
    if upload_file is not None:
        return upload_file.name
    elif dropdown_value:
        return dropdown_value
    else:
        return None

def run_coarse_screening_with_progress(main_dropdown, findings_dropdown, main_upload, findings_upload, system_prompt, progress=gr.Progress()):
    def progress_callback(prog, desc):
        progress(prog, desc=desc)
    
    main_file = get_file_path(main_dropdown, main_upload)
    findings_file = get_file_path(findings_dropdown, findings_upload)
    
    if not main_file:
        return "错误：请选择主会议论文文件"
    
    config = load_config()
    return asyncio.run(coarse_screening(main_file, findings_file, system_prompt, config, progress_callback))

def run_fine_screening_with_progress(input_dropdown, input_upload, system_prompt, progress=gr.Progress()):
    def progress_callback(prog, desc):
        progress(prog, desc=desc)
    
    input_file = get_file_path(input_dropdown, input_upload)
    
    if not input_file:
        return "错误：请选择输入文件"
    
    config = load_config()
    return asyncio.run(fine_screening(input_file, system_prompt, config, progress_callback))

# 创建Gradio界面
def create_interface():
    config = load_config()
    
    with gr.Blocks(title="论文筛选系统", theme=gr.themes.Soft()) as app:
        gr.Markdown("# 🔍 论文筛选系统")
        gr.Markdown("支持粗筛和精排两个阶段的论文筛选，使用大语言模型进行智能分析")
        
        with gr.Tabs():
            # 配置标签页
            with gr.TabItem("⚙️ 配置"):
                gr.Markdown("### 大模型配置")
                gr.Markdown("⚠️ **注意**：请确保API密钥和URL的正确性")
                
                with gr.Row():
                    with gr.Column():
                        api_key_input = gr.Textbox(
                            label="API Key",
                            value=config["api_key"],
                            type="password"
                        )
                        base_url_input = gr.Textbox(
                            label="Base URL",
                            value=config["base_url"]
                        )
                        model_input = gr.Textbox(
                            label="模型名称",
                            value=config["model"]
                        )
                    
                    with gr.Column():
                        rounds_input = gr.Slider(
                            label="处理轮数",
                            minimum=1,
                            maximum=10,
                            step=1,
                            value=config.get("rounds", 3),
                            info="设置筛选的轮数，多轮筛选可以提高准确性"
                        )
                        max_concurrent_input = gr.Slider(
                            label="最大并发数",
                            minimum=1,
                            maximum=200,
                            step=1,
                            value=config.get("max_concurrent", 50),
                            info="⚠️ 注意：API提供商可能有并发限制，建议从较小值开始测试"
                        )
                        
                        gr.Markdown("""
                        **并发说明**：
                        - 并发数过高可能导致API限流
                        - 调用失败时系统会自动暂停1分钟后重试
                        - 精排阶段会自动降低并发数以提高稳定性
                        """)
                
                save_config_btn = gr.Button("💾 保存配置", variant="primary")
                config_status = gr.Textbox(label="状态", interactive=False)
                
                save_config_btn.click(
                    save_config,
                    inputs=[api_key_input, base_url_input, model_input, rounds_input, max_concurrent_input],
                    outputs=config_status
                )
            
            # 粗筛标签页
            with gr.TabItem("🔍 粗筛"):
                gr.Markdown("### 粗筛阶段")
                gr.Markdown("基于论文标题进行初步筛选，快速过滤大量论文")
                
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("**主会议论文文件**")
                        main_file_dropdown = gr.Dropdown(
                            # --- 修改点 3: 使用新函数初始化列表 ---
                            choices=get_json_files(),
                            label="从列表选择",
                            info="从主目录或 arxiv_papers_new 目录选择文件"
                        )
                        main_file_upload = gr.File(
                            label="或从系统选择",
                            file_types=[".json"]
                        )
                        
                        gr.Markdown("**Findings论文文件（可选）**")
                        findings_file_dropdown = gr.Dropdown(
                            # --- 修改点 3: 使用新函数初始化列表 ---
                            choices=[""] + get_json_files(),
                            label="从列表选择",
                            info="从主目录或 arxiv_papers_new 目录选择文件"
                        )
                        findings_file_upload = gr.File(
                            label="或从系统选择",
                            file_types=[".json"]
                        )
                        
                        refresh_files_btn = gr.Button("🔄 刷新文件列表")
                        
                    with gr.Column(scale=2):
                        coarse_prompt = gr.Textbox(
                            label="粗筛提示词",
                            value=COARSE_SYSTEM_PROMPT,
                            lines=8,
                            info="⚠️ 请保持输出格式 <True/False> 不变"
                        )
                
                run_coarse_btn = gr.Button("🚀 开始粗筛", variant="primary", size="lg")
                coarse_output = gr.Textbox(
                    label="粗筛结果",
                    lines=12,
                    interactive=False
                )
                
                run_coarse_btn.click(
                    run_coarse_screening_with_progress,
                    inputs=[main_file_dropdown, findings_file_dropdown, main_file_upload, findings_file_upload, coarse_prompt],
                    outputs=coarse_output
                )
            
            # 精排标签页
            with gr.TabItem("🎯 精排"):
                gr.Markdown("### 精排阶段")
                gr.Markdown("基于论文标题和摘要进行精细筛选，提高筛选质量")
                
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("**粗筛结果文件**")
                        input_file_dropdown = gr.Dropdown(
                            # --- 修改点 3: 使用新函数初始化列表 ---
                            choices=get_result_files(),
                            label="从列表选择",
                            info="选择当前目录下的粗筛结果文件"
                        )
                        input_file_upload = gr.File(
                            label="或从系统选择",
                            file_types=[".json"]
                        )
                        refresh_input_files_btn = gr.Button("🔄 刷新结果文件")
                        
                    with gr.Column(scale=2):
                        fine_prompt = gr.Textbox(
                            label="精排提示词",
                            value=FINE_SYSTEM_PROMPT,
                            lines=10,
                            info="⚠️ 请保持输出格式 <True/False> 不变"
                        )
                
                run_fine_btn = gr.Button("🎯 开始精排", variant="primary", size="lg")
                fine_output = gr.Textbox(
                    label="精排结果",
                    lines=12,
                    interactive=False
                )
                
                run_fine_btn.click(
                    run_fine_screening_with_progress,
                    inputs=[input_file_dropdown, input_file_upload, fine_prompt],
                    outputs=fine_output
                )
            
            # 帮助标签页
            with gr.TabItem("❓ 帮助"):
                gr.Markdown("""
                ### 使用说明
                
                #### 1. 配置设置
                - 在"配置"标签页中设置您的API密钥、Base URL和模型名称
                - **处理轮数**：默认3轮，多轮处理可以提高筛选的准确性和召回率
                - **最大并发数**：控制同时发送的API请求数量，建议从50开始测试
                - 配置会自动保存到 `config.json` 文件中
                
                #### 2. 粗筛流程
                - 从下拉列表选择论文JSON文件（可来自主目录或`arxiv_papers_new`子目录）
                - 可选择添加Findings论文文件
                - 系统会进行多轮筛选并取并集作为最终结果
                - 结果保存为 `原文件名_coarse_final.json`
                
                #### 3. 精排流程
                - 选择粗筛阶段的输出文件
                - 基于标题和摘要进行更精确的筛选
                - 同样进行多轮筛选并取并集
                - 结果保存为 `原文件名_fine_final.json`
                
                #### 4. 文件命名规则
                - 粗筛结果：`原文件名_coarse_final.json`
                - 精排结果：`原文件名_fine_final.json`
                - 中间结果：`原文件名_coarse_round_X.json` / `原文件名_fine_round_X.json`
                """)
        
        # --- 修改点 4: 更新刷新函数以调用新函数 ---
        def refresh_files():
            """刷新所有文件列表"""
            input_files = get_json_files()
            result_files = get_result_files()
            return (
                gr.update(choices=input_files), 
                gr.update(choices=[""] + input_files), 
                gr.update(choices=result_files)
            )
        
        refresh_files_btn.click(
            refresh_files,
            outputs=[main_file_dropdown, findings_file_dropdown, input_file_dropdown]
        )
        
        refresh_input_files_btn.click(
            refresh_files,
            outputs=[main_file_dropdown, findings_file_dropdown, input_file_dropdown]
        )
    
    return app

if __name__ == "__main__":
    if not os.path.exists("arxiv_papers_new"):
        os.makedirs("arxiv_papers_new")

    app = create_interface()
    app.launch(
        server_name="0.0.0.0",
        server_port=7898,
        share=False,
        show_error=True
    )
