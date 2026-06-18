"""从JavDB抓取数据 - 多域名降级版"""
import os
import re
import time
import random
import logging
from typing import Optional, List, Dict

from javsp.web.base import Request, resp2html
from javsp.web.exceptions import *
from javsp.func import *
from javsp.avid import guess_av_type
from javsp.config import Cfg, CrawlerID
from javsp.datatype import MovieInfo, GenreMap
from javsp.chromium import get_browsers_cookies


logger = logging.getLogger(__name__)

# 用户代理列表，用于降低被风控概率
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
]


class JavDBBrowser:
    """JavDB 浏览器封装类，处理多域名降级和请求管理"""

    def __init__(self):
        self.domains: List[str] = Cfg().crawler.javdb_domain_list
        self.current_domain_index: int = 0
        self.request: Optional[Request] = None
        self.cookies_pool: List[Dict] = []
        self.genre_map = GenreMap('data/genre_javdb.csv')
        self._init_request()

    def _init_request(self) -> None:
        """初始化 Request 实例，配置完整的浏览器头"""
        self.request = Request(use_scraper=True)
        # 完整的浏览器请求头
        self.request.headers.update({
            'Accept-Language': 'zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.7,en;q=0.6,ja;q=0.5',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        })
        # 随机 User-Agent
        self.request.headers['User-Agent'] = random.choice(USER_AGENTS)

    def _refresh_headers(self) -> None:
        """刷新请求头（用于重试时更换 UA）"""
        self.request.headers['User-Agent'] = random.choice(USER_AGENTS)

    def _get_current_domain(self) -> str:
        """获取当前域名"""
        if self.current_domain_index < len(self.domains):
            return self.domains[self.current_domain_index]
        # 如果所有域名都失败，循环回第一个
        return self.domains[0]

    def _normalize_url(self, url: str) -> str:
        """标准化 URL，确保带有协议前缀"""
        if not url.startswith('http'):
            url = 'https://' + url
        return url

    def _ensure_cookies(self) -> bool:
        """确保有可用的 Cookies"""
        if self.cookies_pool:
            return True

        try:
            self.cookies_pool = get_browsers_cookies()
        except (PermissionError, OSError) as e:
            logger.warning(f"无法从浏览器 Cookies 文件获取 JavDB 登录凭据: {e}，可能是安全软件在保护浏览器 Cookies 文件")
            self.cookies_pool = []
        except Exception as e:
            logger.warning(f"获取 JavDB Cookies 时出错: {e}，你可能使用的是国内定制版等非官方 Chrome 系浏览器")
            self.cookies_pool = []

        return len(self.cookies_pool) > 0

    def _apply_cookies(self) -> bool:
        """应用 Cookies 到请求，返回是否成功"""
        if not self._ensure_cookies():
            return False

        if self.cookies_pool:
            item = self.cookies_pool.pop()
            self._init_request()
            self.request.cookies = item['cookies']
            logger.debug(f'应用浏览器 Cookies: {item["profile"]} - {item["site"]}')
            return True
        return False

    def fetch(self, path: str) -> object:
        """带多域名降级和重试的 HTML 获取

        Args:
            path: 请求路径，如 '/search?q=ABC-123'

        Returns:
            HtmlElement: 解析后的 HTML 文档

        Raises:
            SiteBlocked: 所有域名和重试次数耗尽后抛出
        """
        errors = []  # 记录所有域名的失败原因
        max_retries_per_domain = 2  # 每个域名最多重试次数

        while self.current_domain_index < len(self.domains):
            domain = self._normalize_url(self._get_current_domain())
            full_url = f'{domain}{path}'
            logger.debug(f'尝试访问 JavDB: {full_url}')

            for retry in range(max_retries_per_domain):
                try:
                    # 随机延迟，降低请求频率特征
                    if retry > 0:
                        time.sleep(random.uniform(0.5, 1.5))

                    r = self.request.get(full_url, delay_raise=True)

                    # 处理 200 响应
                    if r.status_code == 200:
                        # 检查是否被重定向到登录页
                        if r.history and '/login' in r.url:
                            logger.debug(f'{domain}: 被重定向到登录页')
                            if self._apply_cookies():
                                self._refresh_headers()
                                continue  # 重试
                            raise CredentialError('JavDB: 所有浏览器 Cookies 均已过期')

                        # 检查是否被重定向到付费页面
                        if r.history and 'pay' in r.url.split('/')[-1]:
                            raise SitePermissionError(f'JavDB: 此资源被限制为仅 VIP 可见: {r.history[0].url}')

                        logger.info(f'JavDB 访问成功: {domain}')
                        return resp2html(r)

                    # 处理 403/503 响应
                    elif r.status_code in (403, 503):
                        html = resp2html(r)
                        code_tag = html.xpath("//span[@class='code-label']/span")
                        error_code = code_tag[0].text if code_tag else None

                        if r.status_code == 403:
                            error_msg = f'{domain}: 403 禁止访问'
                            if error_code:
                                error_msg += f' (Error code: {error_code})'
                                if error_code == '1020':
                                    error_msg = f'{domain}: 1020 错误 - 可能被日本 IP 封锁，请使用其他地区代理'
                            logger.warning(error_msg)
                            errors.append(error_msg)

                            # 1020 错误通常是 IP 地区问题，切换域名
                            if error_code == '1020':
                                break
                        else:
                            error_msg = f'{domain}: 503 服务不可用'
                            logger.warning(error_msg)
                            errors.append(error_msg)

                        # 刷新 UA 重试
                        self._refresh_headers()
                        self._init_request()
                        continue

                    # 其他状态码
                    else:
                        error_msg = f'{domain}: {r.status_code} 非预期状态码'
                        logger.warning(error_msg)
                        errors.append(error_msg)
                        raise WebsiteError(error_msg)

                except requests.exceptions.Timeout:
                    error_msg = f'{domain}: 请求超时'
                    logger.warning(error_msg)
                    errors.append(error_msg)
                    self._refresh_headers()
                    continue

                except requests.exceptions.ConnectionError as e:
                    error_msg = f'{domain}: 连接错误 - {repr(e)}'
                    logger.warning(error_msg)
                    errors.append(error_msg)
                    self._refresh_headers()
                    continue

                except SitePermissionError:
                    raise

                except CredentialError:
                    raise

                except Exception as e:
                    error_msg = f'{domain}: {type(e).__name__} - {repr(e)}'
                    logger.warning(f'JavDB 请求异常: {error_msg}')
                    errors.append(error_msg)
                    break

            # 当前域名失败，切换到下一个
            self.current_domain_index += 1
            self._init_request()

        # 所有域名都失败
        error_summary = '; '.join(errors)
        raise SiteBlocked(f'JavDB: 所有域名均访问失败。错误汇总: {error_summary}')


class JavDBCrawler:
    """JavDB 爬虫类，封装所有抓取逻辑"""

    def __init__(self):
        self.browser = JavDBBrowser()
        self.genre_map = self.browser.genre_map
        self.domains = self.browser.domains
        self.permanent_url = 'https://javdb.com'

    def parse_data(self, movie: MovieInfo) -> None:
        """从网页抓取并解析指定番号的数据

        Args:
            movie: MovieInfo 实例，解析后的信息直接更新到此变量内
        """
        # 搜索番号
        html = self.browser.fetch(f'/search?q={movie.dvdid}')
        ids = list(map(str.lower, html.xpath("//div[@class='video-title']/strong/text()")))
        movie_urls = html.xpath("//a[@class='box']/@href")
        match_count = len([i for i in ids if i == movie.dvdid.lower()])

        if match_count == 0:
            raise MovieNotFoundError(__name__, movie.dvdid, ids)
        elif match_count > 1:
            raise MovieDuplicateError(__name__, movie.dvdid, match_count)

        index = ids.index(movie.dvdid.lower())
        new_url = movie_urls[index]

        try:
            html2 = self.browser.fetch(new_url)
        except (SitePermissionError, CredentialError):
            # VIP 内容，降级获取搜索页信息
            box = html.xpath("//a[@class='box']")[index]
            movie.url = new_url
            movie.title = box.get('title')
            movie.cover = box.xpath("div/img/@src")[0]
            score_str = box.xpath("div[@class='score']/span/span")[0].tail
            score = re.search(r'([\d.]+)分', score_str).group(1)
            movie.score = "{:.2f}".format(float(score) * 2)
            movie.publish_date = box.xpath("div[@class='meta']/text()")[0].strip()
            return

        # 解析详情页
        container = html2.xpath("/html/body/section/div/div[@class='video-detail']")[0]
        info = container.xpath("//nav[@class='panel movie-panel-info']")[0]
        title = container.xpath("h2/strong[@class='current-title']/text()")[0]

        show_orig_title = container.xpath("//a[contains(@class, 'meta-link') and not(contains(@style, 'display: none'))]")
        if show_orig_title:
            movie.ori_title = container.xpath("h2/span[@class='origin-title']/text()")[0]

        cover = container.xpath("//img[@class='video-cover']/@src")[0]
        preview_pics = container.xpath("//a[@class='tile-item'][@data-fancybox='gallery']/@href")
        preview_video_tag = container.xpath("//video[@id='preview-video']/source/@src")
        if preview_video_tag:
            preview_video = preview_video_tag[0]
            if preview_video.startswith('//'):
                preview_video = 'https:' + preview_video
            movie.preview_video = preview_video

        dvdid = info.xpath("div/span")[0].text_content()
        publish_date = info.xpath("div/strong[text()='日期:']")[0].getnext().text
        duration = info.xpath("div/strong[text()='時長:']")[0].getnext().text.replace('分鍾', '').strip()

        director_tag = info.xpath("div/strong[text()='導演:']")
        if director_tag:
            movie.director = director_tag[0].getnext().text_content().strip()

        av_type = guess_av_type(movie.dvdid)
        if av_type != 'fc2':
            producer_tag = info.xpath("div/strong[text()='片商:']")
        else:
            producer_tag = info.xpath("div/strong[text()='賣家:']")
        if producer_tag:
            movie.producer = producer_tag[0].getnext().text_content().strip()

        publisher_tag = info.xpath("div/strong[text()='發行:']")
        if publisher_tag:
            movie.publisher = publisher_tag[0].getnext().text_content().strip()

        serial_tag = info.xpath("div/strong[text()='系列:']")
        if serial_tag:
            movie.serial = serial_tag[0].getnext().text_content().strip()

        score_tag = info.xpath("//span[@class='score-stars']")
        if score_tag:
            score_str = score_tag[0].tail
            score = re.search(r'([\d.]+)分', score_str).group(1)
            movie.score = "{:.2f}".format(float(score) * 2)

        genre_tags = info.xpath("//strong[text()='類別:']/../span/a")
        genre, genre_id = [], []
        for tag in genre_tags:
            pre_id = tag.get('href').split('/')[-1]
            genre.append(tag.text)
            genre_id.append(pre_id)
            subsite = pre_id.split('?')[0]
            movie.uncensored = {'uncensored': True, 'tags': False}.get(subsite)

        actors_tag = info.xpath("//strong[text()='演員:']/../span")[0]
        all_actors = actors_tag.xpath("a/text()")
        genders = actors_tag.xpath("strong/text()")
        actress = [i for i in all_actors if genders[all_actors.index(i)] == '♀']
        magnet = container.xpath("//div[@class='magnet-name column is-four-fifths']/a/@href")

        movie.dvdid = dvdid
        # 将当前使用的域名替换为永久域名
        current_domain = self.browser._normalize_url(self.browser._get_current_domain())
        movie.url = new_url.replace(current_domain, self.permanent_url)
        movie.title = title.replace(dvdid, '').strip()
        movie.cover = cover
        movie.preview_pics = preview_pics
        movie.publish_date = publish_date
        movie.duration = duration
        movie.genre = genre
        movie.genre_id = genre_id
        movie.actress = actress
        movie.magnet = [i.replace('[javdb.com]', '') for i in magnet]

    def parse_clean_data(self, movie: MovieInfo) -> None:
        """解析指定番号的影片数据并进行清洗"""
        try:
            self.parse_data(movie)
            # 检查封面 URL 是否真的存在
            if movie.cover is not None:
                r = self.browser.request.head(movie.cover)
                if r.status_code != 200:
                    movie.cover = None
        except SiteBlocked:
            raise
        except Exception as e:
            logger.error(f'JavDB: 解析数据时出错: {e}')
            raise

        if movie.genre_id and not movie.genre_id[0].startswith('fc2?'):
            movie.genre_norm = self.genre_map.map(movie.genre_id)
            movie.genre_id = None


# 全局爬虫实例（懒加载，兼容旧接口）
_crawler_instance: Optional[JavDBCrawler] = None


def _get_crawler() -> JavDBCrawler:
    """获取爬虫实例"""
    global _crawler_instance
    if _crawler_instance is None:
        _crawler_instance = JavDBCrawler()
    return _crawler_instance


# ============ 兼容旧接口的函数 ============

def parse_data(movie: MovieInfo) -> None:
    """从网页抓取并解析指定番号的数据（兼容旧接口）"""
    _get_crawler().parse_data(movie)


def parse_clean_data(movie: MovieInfo) -> None:
    """解析指定番号的影片数据并进行清洗（兼容旧接口）"""
    _get_crawler().parse_clean_data(movie)


# ============ 女优别名收集功能 ============

def collect_actress_alias(type: int = 0, use_original: bool = True) -> None:
    """收集女优的别名

    Args:
        type: 0-有码, 1-无码, 2-欧美
        use_original: 是否使用原名而非译名，True-田中レモン，False-田中檸檬
    """
    import json
    import random

    actress_alias_map: Dict[str, List[str]] = {}
    actress_alias_file_path = "data/actress_alias.json"

    # 确保文件存在
    if not os.path.exists(actress_alias_file_path):
        with open(actress_alias_file_path, "w", encoding="utf-8") as f:
            json.dump({}, f)

    type_list = ["censored", "uncensored", "western"]
    crawler = _get_crawler()
    domain = crawler.domains[0] if crawler.domains else 'https://javdb.com'
    page_url = f"{domain}/actors/{type_list[type]}"

    while True:
        try:
            html = crawler.browser.fetch(page_url)
            actors = html.xpath("//div[@class='box actor-box']/a")

            count = 0
            for actor in actors:
                count += 1
                actor_name = actor.xpath("strong/text()")[0].strip()
                actor_url = actor.xpath("@href")[0]

                # 进入演员主页
                actor_html = crawler.browser.fetch(actor_url)

                # 解析演员所有名字信息
                names_span = actor_html.xpath("//span[@class='actor-section-name']")[0]
                aliases_span_list = actor_html.xpath("//span[@class='section-meta']")
                aliases_span = aliases_span_list[0]

                names_list = [name.strip() for name in names_span.text.split(",")]
                if len(aliases_span_list) > 1:
                    aliases_list = [alias.strip() for alias in aliases_span.text.split(",")]
                else:
                    aliases_list = []

                actress_alias_map[names_list[-1 if use_original else 0]] = names_list + aliases_list
                print(f"{count} --- {names_list[-1 if use_original else 0]}: {names_list + aliases_list}")

                if count == 10:
                    # 写入文件
                    with open(actress_alias_file_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    existing_data.update(actress_alias_map)
                    with open(actress_alias_file_path, "w", encoding="utf-8") as f:
                        json.dump(existing_data, f, ensure_ascii=False, indent=2)

                    actress_alias_map = {}
                    print(f"已爬取 {count} 个女优，数据已更新并写回文件: {actress_alias_file_path}")
                    count = 0

                time.sleep(max(1, 10 * random.random()))

            # 下一页
            next_page_link = html.xpath("//a[@rel='next' and @class='pagination-next']/@href")
            if not next_page_link:
                break
            page_url = next_page_link[0]

        except SiteBlocked:
            raise

    # 最终写入
    with open(actress_alias_file_path, "r", encoding="utf-8") as f:
        existing_data = json.load(f)
    existing_data.update(actress_alias_map)
    with open(actress_alias_file_path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f, ensure_ascii=False, indent=2)

    print(f"已爬取 {count} 个女优，数据已更新并写回文件: {actress_alias_file_path}")


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    # 测试用例
    test_cases = ['IPX-177', 'FC2-2735981', 'ABC-123']

    for avid in test_cases:
        print(f"\n{'='*60}")
        print(f"测试番号: {avid}")
        print('='*60)
        movie = MovieInfo(avid)
        try:
            parse_clean_data(movie)
            print(movie)
        except CrawlerError as e:
            print(f"抓取失败: {repr(e)}")
