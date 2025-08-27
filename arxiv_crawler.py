import os
import json
import time
import random
import logging
from datetime import datetime, timedelta, timezone 
from pathlib import Path
import schedule
import arxiv

# 配置日志 - 解决中文乱码问题
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("arxiv_crawler.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# 基本配置
class Config:
    BASE_DIR = Path("arxiv_papers_new")
    LAST_CRAWL_TIME_PATH = BASE_DIR / "last_crawl_time.json"
    FAILED_INTERVALS_PATH = BASE_DIR / "failed_intervals.json"
    
    KEYWORDS = [
        "LLM", 
        "large language model"
    ]
    
    # 统一使用带时区的datetime对象
    START_DATE_2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    END_DATE_2024 = datetime(2024, 12, 31, tzinfo=timezone.utc)
    START_DATE_2025 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    
    # 调整爬取参数
    MAX_RESULTS_PER_REQUEST = 25
    BASE_DELAY = 30
    MAX_RETRIES = 5
    API_RESULT_LIMIT = 800
    INCREMENTAL_CHECK_HOUR = 12 
    
    @classmethod
    def ensure_directories(cls):
        if not cls.BASE_DIR.exists():
            cls.BASE_DIR.mkdir(parents=True, exist_ok=True)
        if not cls.FAILED_INTERVALS_PATH.exists():
            with open(cls.FAILED_INTERVALS_PATH, 'w', encoding='utf-8') as f:
                json.dump([], f, ensure_ascii=False)
    
    @classmethod
    def save_failed_interval(cls, start, end, error):
        failed = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "error": str(error),
            # 优化：统一使用UTC时间记录
            "record_time": datetime.now(timezone.utc).isoformat()
        }
        # 优化：使用更安全的文件写入方式，避免'r+'模式的风险
        try:
            with open(cls.FAILED_INTERVALS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        
        data.append(failed)
        
        with open(cls.FAILED_INTERVALS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logging.warning(f"已记录失败区间: {start.date()} 至 {end.date()}")
    
    @classmethod
    def save_last_crawl_time(cls, crawl_time=None):
        # 优化：如果传入的是naive time，则假定为本地时间并转为UTC；否则使用传入的aware time
        if crawl_time is None:
            crawl_time = datetime.now(timezone.utc)
        elif crawl_time.tzinfo is None:
             # 假定 naive datetime 是本地时间，转换为 aware UTC time
            crawl_time = crawl_time.astimezone(timezone.utc)

        with open(cls.LAST_CRAWL_TIME_PATH, 'w', encoding='utf-8') as f:
            json.dump({"last_crawl": crawl_time.isoformat()}, f, ensure_ascii=False)
    
    @classmethod
    def load_last_crawl_time(cls):
        if cls.LAST_CRAWL_TIME_PATH.exists():
            try:
                with open(cls.LAST_CRAWL_TIME_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # fromisoformat可以直接转换带时区信息的字符串
                    return datetime.fromisoformat(data["last_crawl"])
            except (json.JSONDecodeError, KeyError):
                logging.warning("上次爬取时间文件损坏，使用默认值")
        # 返回带时区的默认值
        return cls.START_DATE_2025


# 数据处理函数
def format_paper_data(arxiv_result):
    # 优化：直接使用从API获取的、带时区的datetime对象
    return {
        "title": arxiv_result.title.strip(),
        "abstract": arxiv_result.summary.strip(),
        "authors": [author.name for author in arxiv_result.authors],
        "published": arxiv_result.published.isoformat(),
        "updated": arxiv_result.updated.isoformat(),
        "arxiv_id": arxiv_result.entry_id.split('/')[-1],
        "url": arxiv_result.pdf_url,
        "categories": arxiv_result.categories,
        "primary_category": arxiv_result.primary_category
    }

def get_file_path_for_date(publish_date):
    year = publish_date.year
    month = publish_date.month
    if year == 2024:
        return Config.BASE_DIR / "arxiv_2024_llm_papers.json"
    elif year == 2025:
        return Config.BASE_DIR / f"arxiv_2025_{month:02d}_llm_papers.json"
    else:
        return Config.BASE_DIR / f"arxiv_{year}_{month:02d}_llm_papers.json"

def load_existing_papers(file_path):
    if file_path.exists():
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('papers', [])
        except json.JSONDecodeError:
            logging.warning(f"文件 {file_path} 格式错误，将创建新文件")
            return []
    return []

def save_papers_to_file(papers, file_path):
    sorted_papers = sorted(
        papers, 
        key=lambda x: x['published'], 
        reverse=True
    )
    data = {
        "metadata": {
            # 优化：统一使用UTC时间记录
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_papers": len(sorted_papers),
            "source": "arXiv",
            "keywords": Config.KEYWORDS
        },
        "papers": sorted_papers
    }
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logging.info(f"已保存 {len(sorted_papers)} 篇论文到 {file_path}")

def add_new_papers(new_papers):
    papers_to_add_by_file = {}
    for paper in new_papers:
        publish_date = datetime.fromisoformat(paper['published'])
        file_path = get_file_path_for_date(publish_date)
        if file_path not in papers_to_add_by_file:
            papers_to_add_by_file[file_path] = []
        papers_to_add_by_file[file_path].append(paper)

    for file_path, papers in papers_to_add_by_file.items():
        existing_papers = load_existing_papers(file_path)
        existing_ids = {p['arxiv_id'] for p in existing_papers}
        
        unique_new = [p for p in papers if p['arxiv_id'] not in existing_ids]
        
        if unique_new:
            all_papers = existing_papers + unique_new
            save_papers_to_file(all_papers, file_path)
            logging.info(f"成功向 {file_path} 添加了 {len(unique_new)} 篇新论文。")

# 核心爬取函数
def search_arxiv_papers(start_date, end_date, max_results=None):
    keyword_queries = [f'ti:"{kw}"' for kw in Config.KEYWORDS] + [f'abs:"{kw}"' for kw in Config.KEYWORDS]
    query = f"({ ' OR '.join(keyword_queries) })"
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")
    query += f" AND submittedDate:[{start_str} TO {end_str}]"
    logging.info(f"搜索查询: 时间范围：{start_date.date()} 至 {end_date.date()}")
    
    client = arxiv.Client(
        page_size=Config.MAX_RESULTS_PER_REQUEST,
        delay_seconds=Config.BASE_DELAY,
        num_retries=Config.MAX_RETRIES
    )
    
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending
    )
    
    papers = []
    try:
        for result in client.results(search):
            paper_data = format_paper_data(result)
            papers.append(paper_data)
            
            if len(papers) % 50 == 0:
                logging.info(f"已找到 {len(papers)} 篇论文...")
            
            if max_results and len(papers) >= max_results:
                break
    
    except Exception as e:
        logging.error(f"搜索过程中发生无法恢复的错误: {str(e)}，已获取 {len(papers)} 篇论文")
        Config.save_failed_interval(start_date, end_date, str(e))
    
    return papers


# 时间范围拆分工具
def split_time_range(start, end):
    ranges = []
    current_start = start
    
    while current_start <= end:
        # 这样可以确保当天的数据能被完整获取。
        current_end = current_start + timedelta(days=1)
        # # 防止结束日期超出总范围
        # if current_end > end:
        #     current_end = end
            
        ranges.append((current_start, current_end))
        
        # 修正：循环步进一天，确保不会跳日。
        # 虽然会有1天的重叠查询，但add_new_papers函数会去重。
        current_start = current_start + timedelta(days=1)

        # 如果开始日期已经等于结束日期，说明已经处理了最后一天，可以退出循环
        if current_start > current_end:
            break
    
    return ranges


# 全量/增量爬取函数
def full_crawl_2024():
    logging.info("开始全量爬取2024年的论文...")
    time_ranges = split_time_range(Config.START_DATE_2024, Config.END_DATE_2024)
    
    for i, (start, end) in enumerate(time_ranges):
        try:
            logging.info(f"处理第 {i+1}/{len(time_ranges)} 个区间: {start.date()} 至 {end.date()}")
            papers = search_arxiv_papers(start, end)
            if papers:
                add_new_papers(papers)
        except Exception as e:
            logging.error(f"区间 {start.date()}~{end.date()} 爬取失败: {str(e)}，继续下一个区间")
            Config.save_failed_interval(start, end, str(e))
    
    logging.info("2024年论文全量爬取完成")

def full_crawl_2025_until_now():
    logging.info("开始全量爬取2025年至当前日期的论文...")
    # 优化：统一使用UTC时间
    current_date = datetime.now(timezone.utc)
    
    # 动态确定要爬取的月份范围
    start_month_date = Config.START_DATE_2025
    while start_month_date <= current_date:
        year = start_month_date.year
        month = start_month_date.month
        
        # 计算当月最后一天
        if month == 12:
            end_of_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        else:
            end_of_month = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        
        end_date_for_month = min(end_of_month, current_date)
        
        logging.info(f"开始处理 {year}年{month}月 的论文...")
        time_ranges = split_time_range(start_month_date, end_date_for_month)
        for sub_start, sub_end in time_ranges:
            try:
                papers = search_arxiv_papers(sub_start, sub_end)
                if papers:
                    add_new_papers(papers)
            except Exception as e:
                logging.error(f"子区间 {sub_start.date()}~{sub_end.date()} 爬取失败: {str(e)}，继续下一个子区间")
                Config.save_failed_interval(sub_start, sub_end, str(e))
        
        # 前进到下一个月的第一天
        start_month_date = end_of_month + timedelta(days=1)

    Config.save_last_crawl_time(current_date)
    logging.info("2025年至当前日期的论文全量爬取完成")

def incremental_crawl():
    """
    执行延时增量爬取，并自动追赶错过的日期。
    1. 追赶爬取：如果距离上次成功爬取有时间空缺，会从上次结束点一直爬取到延时窗口的开始。
    2. 常规延时爬取：爬取四天前中午12点到三天前中午12点（UTC时间）这个24小时窗口的数据。
    """
    last_run_time = Config.load_last_crawl_time()
    current_time = datetime.now(timezone.utc)
    
    # 检查上次运行时间，避免在1小时内重复执行
    if (current_time - last_run_time) < timedelta(hours=1):
        logging.info("距离上次爬取不足1小时，跳过本次增量爬取")
        return

    # --- 1. 定义常规延时爬取的窗口 ---
    today_at_crawl_hour = current_time.replace(hour=Config.INCREMENTAL_CHECK_HOUR, minute=0, second=0, microsecond=0)
    
    # 常规窗口的开始时间 = (今天中午12点) - 4天
    delayed_window_start = today_at_crawl_hour - timedelta(days=4)
    # 常规窗口的结束时间 = (今天中午12点) - 3天
    delayed_window_end = today_at_crawl_hour - timedelta(days=3)

    # --- 2. 检查并执行“追赶爬取” ---
    # 计算上次运行时，它所爬取的那个延时窗口的结束时间点
    last_run_as_today = last_run_time.replace(hour=Config.INCREMENTAL_CHECK_HOUR, minute=0, second=0, microsecond=0)
    last_crawled_window_end = last_run_as_today - timedelta(days=3)
    
    # 如果上次成功爬取的窗口结束点，早于我们本次常规窗口的开始点，说明中间有数据空缺
    if last_crawled_window_end < delayed_window_start:
        # 追赶的开始时间，就是上次成功爬取窗口的结束点
        catch_up_start = last_crawled_window_end
        # 追赶的结束时间，就是本次常规窗口的开始点
        catch_up_end = delayed_window_start

        logging.info(f"检测到数据空缺，开始追赶爬取: {catch_up_start.isoformat()} 至 {catch_up_end.isoformat()}")
        
        # 使用split_time_range来处理可能长达数天的追赶窗口
        time_ranges = split_time_range(catch_up_start, catch_up_end)
        for start, end in time_ranges:
            try:
                logging.info(f"追赶处理区间: {start.date()} 至 {end.date()}")
                papers = search_arxiv_papers(start, end)
                if papers:
                    add_new_papers(papers)
            except Exception as e:
                logging.error(f"追赶区间 {start.date()}~{end.date()} 爬取失败: {str(e)}，继续下一个区间")
                Config.save_failed_interval(start, end, str(e))
        
        logging.info("追赶爬取完成。")

    # --- 3. 执行常规的延时增量爬取 ---
    logging.info(f"开始常规延时增量爬取，目标日期窗口: {delayed_window_start.isoformat()} 至 {delayed_window_end.isoformat()}")
    
    try:
        new_papers = search_arxiv_papers(delayed_window_start, delayed_window_end)
        if new_papers:
            logging.info(f"在常规延时窗口中发现 {len(new_papers)} 篇新论文，正在添加...")
            add_new_papers(new_papers)
        else:
            logging.info(f"在常规延时窗口中未发现新论文")
    except Exception as e:
        logging.error(f"常规延时增量爬取窗口 {delayed_window_start.date()}~{delayed_window_end.date()} 失败: {str(e)}")
        Config.save_failed_interval(delayed_window_start, delayed_window_end, str(e))
    
    # 4. 无论如何，都保存当前时间作为“最后一次运行”的时间戳
    Config.save_last_crawl_time(current_time)


# 定时任务与主函数
def setup_scheduled_tasks():
    schedule.every().day.at(f"{Config.INCREMENTAL_CHECK_HOUR:02}:00").do(incremental_crawl)
    logging.info(f"已设置定时任务：每天 {Config.INCREMENTAL_CHECK_HOUR:02}:00 执行增量爬取")
    return schedule

def run_scheduler_continuously(schedule_instance, interval=1):
    logging.info("开始运行定时任务调度器...")
    try:
        while True:
            schedule_instance.run_pending()
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("用户中断，程序退出")
    except Exception as e:
        logging.error(f"调度器运行出错: {str(e)}，程序将退出。")


def main(skip_full_crawl=False):
    logging.info("=== 启动arXiv论文爬取系统（最终优化版） ===")
    Config.ensure_directories()
    
    if not skip_full_crawl:
        try:
            full_crawl_2024()
            full_crawl_2025_until_now()
        except Exception as e:
            logging.error(f"全量爬取过程中发生严重错误: {str(e)}，继续执行定时任务...")
    
    # --- 启动时立即检查 ---
    # 这是一个安全机制，确保如果程序长时间关闭后重启，
    # 它会立即执行一次增量任务来追赶错过的日期，而无需等待下一个预定时间。
    last_run = Config.load_last_crawl_time()
    if (datetime.now(timezone.utc) - last_run) > timedelta(hours=23):
        logging.info("检测到自上次运行以来已超过23小时，立即执行一次增量任务以追赶数据...")
        try:
            incremental_crawl()
        except Exception as e:
            logging.error(f"启动时的追赶任务失败: {e}")
    
    scheduler = setup_scheduled_tasks()
    run_scheduler_continuously(scheduler)
    
if __name__ == "__main__":
    # 首次运行时，可以设为False来执行全量爬取。
    # 日常运行时，可以设为True来跳过全量爬取，只依赖追赶和定时任务。
    main(skip_full_crawl=True)
