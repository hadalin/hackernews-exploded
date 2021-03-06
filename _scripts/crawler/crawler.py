import argparse
import json
import logging
import os
import re
import requests
import sys
import unicodedata

import timeout_decorator
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from hackernews import HackerNews, InvalidItemID
from tqdm import tqdm
from webpreview import web_preview
from urllib.parse import urlparse, urlunparse


logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = logging.getLogger(__name__)


def get_all_comment_ids(hn, item_id):
  kid_ids = []

  def traverse(traverse_item_id):
    try:
      item = hn.get_item(traverse_item_id)
    except InvalidItemID:
      return

    if item.kids:
      for kid_id in item.kids:
        kid_ids.append(kid_id)
        traverse(kid_id)

  traverse(item_id)
  return kid_ids

def get_item_url(item_id):
  return 'https://news.ycombinator.com/item?id={}'.format(item_id)

def get_urls_from_comment(hn, comment):
  if comment.dead or not comment.text or comment.text == '.':  # Ignore dots because BeautifulSoup thinks it's a filename
    return []

  soup = BeautifulSoup(comment.text, features='lxml')
  anchors = soup.find_all('a')
  return [anchor['href'] for anchor in anchors if anchor and anchor['href']]

def get_urls_from_top_stories(hn, limit):
  logger.info('Get urls from stories')

  def add_to_urls(item_id, root_id, original_title, url, list_to_mutate):
    if not any(item['url'] == url for item in list_to_mutate):
      list_to_mutate.append({
        'item_id': item_id,
        'item_url': get_item_url(item_id),
        'root_id': root_id,
        'original_title': original_title,
        'url': url,
      })

  url_list = []
  for story in tqdm(hn.top_stories(limit=limit)):
    add_to_urls(story.item_id, None, story.title, story.url, url_list)

    story_comment_ids = get_all_comment_ids(hn, story.item_id)
    for comment in hn.get_items_by_ids(story_comment_ids):
      for url in get_urls_from_comment(hn, comment):
        add_to_urls(comment.item_id, story.item_id, None, url, url_list)

  return url_list

def filter_urls(urls):
  filtered = list(filter(lambda x: x['url'], urls))
  filtered = list(filter(lambda x: not x['url'].startswith('https://news.ycombinator.com/item'), filtered))
  filtered = list(filter(lambda x: not urlparse(x['url']).path.endswith('.pdf'), filtered))
  filtered = list(filter(lambda x: not urlparse(x['url']).path.endswith('.mp3'), filtered))

  return filtered

class Metafier():
  @classmethod
  def _compose_metadata(cls, parsed_url, content, category):
    url = urlunparse(parsed_url)
    try:
      title, description = cls._get_site_title_and_description(url, content)
    except timeout_decorator.TimeoutError:
      logger.exception('Timeout for {}'.format(url))
      title, description = '', ''

    return {'title': title, 'description': description, 'url': url, 'hostname': urlparse(url).hostname, 'category': category}

  @staticmethod
  @timeout_decorator.timeout(15)
  def _get_site_title_and_description(url, content):
    try:
      logger.info('Getting metadata for {}'.format(url))
      title, description, _ = web_preview(url, content=content)
      description = description[:450] + '...' if description and len(description) > 450 else description
      return title, description
    except:
      logger.exception('Could not get metadata for {}'.format(url))
      return '', ''

  class Site():
    category = 'site'

    def resolves(self, parsed_url):
      return not parsed_url.path and not parsed_url.query and not parsed_url.fragment

  class Repository():
    category = 'repository'

    path_regex = re.compile(r'^\/[^/]+\/[^/]+/?$')  # Matches /<org>/<repo>

    def resolves(self, parsed_url):
      hostnames = ['github.com', 'gitlab.com', 'bitbucket.org']
      return parsed_url.hostname in hostnames and self.path_regex.match(parsed_url.path)

  class WikiArticle():
    category = 'wiki_article'

    hostname_regex = re.compile(r'^.*wikipedia\.org$')
    path_regex = re.compile(r'^/wiki/[^/]+/?$')  # Matches /wiki/<article>

    def resolves(self, parsed_url):
      return self.hostname_regex.match(parsed_url.hostname) and self.path_regex.match(parsed_url.path)

  class AmazonItem():
    category = 'amazon_item'

    hostname_regex = re.compile(r'^.*amazon\..+$')

    def resolves(self, parsed_url):
      return self.hostname_regex.match(parsed_url.hostname)

  class Video():
    category = 'video'

    # YouTube
    youtube_hostname_regex = re.compile(r'^.*youtube\.com$')
    youtube_path_regex = re.compile(r'^/watch.*$')
    def resolve_youtube(self, parsed_url):
      return self.youtube_hostname_regex.match(parsed_url.hostname) and self.youtube_path_regex.match(parsed_url.path)

    # Vimeo
    vimeo_hostname_regex = re.compile(r'^.*vimeo\.com$')
    vimeo_path_regex = re.compile(r'^/\d+$')
    def resolve_vimeo(self, parsed_url):
      return self.vimeo_hostname_regex.match(parsed_url.hostname) and self.vimeo_path_regex.match(parsed_url.path)

    def resolves(self, parsed_url):
      return self.resolve_youtube(parsed_url) or self.resolve_vimeo(parsed_url)

  class Tweet():
    category = 'tweet'

    hostname_regex = re.compile(r'^.*twitter\.com$')
    path_regex = re.compile(r'^/[^/]+/status/\d+/?$')

    def resolves(self, parsed_url):
      return self.hostname_regex.match(parsed_url.hostname) and self.path_regex.match(parsed_url.path)

  class Subreddit():
    category = 'subreddit'

    hostname_regex = re.compile(r'^.*reddit\.com$')
    path_regex = re.compile(r'^/r/[^/]+/?$')  # https://www.reddit.com/r/ama

    def resolves(self, parsed_url):
      return self.hostname_regex.match(parsed_url.hostname) and self.path_regex.match(parsed_url.path)

  class RedditPost():
    category = 'reddit_post'

    hostname_regex = re.compile(r'^.*reddit\.com$')
    # Post https://www.reddit.com/r/AMA/comments/arkxuh/i_spent_a_year_in_a_bangkok_prison_ama/
    # Comment https://www.reddit.com/r/AMA/comments/arkxuh/i_spent_a_year_in_a_bangkok_prison_ama/ego4cdj/
    path_regex = re.compile(r'^/r/[^/]+/comments/[^/]+/[^/]+/?$')

    def resolves(self, parsed_url):
      return self.hostname_regex.match(parsed_url.hostname) and self.path_regex.match(parsed_url.path)

  class News():
    category = 'news'

    hostname_regexes = [
      re.compile(r'^.*techcrunch\.com$'), re.compile(r'^.*nytimes\.com$'), re.compile(r'^.*bloomberg\.com$'),
      re.compile(r'^.*theverge\.com$'), re.compile(r'^.*theguardian\.com$'), re.compile(r'^.*news\.yahoo\.com$'),
      re.compile(r'^.*washingtonpost\.com$'), re.compile(r'^.*news\.google\.com$'), re.compile(r'^.*huffingtonpost\.com$'),
      re.compile(r'^.*cnn\.com$'), re.compile(r'^.*foxnews\.com$'), re.compile(r'^.*nbcnews\.com$'), re.compile(r'^.*dailymail\.co\.uk$'),
      re.compile(r'^.*wsj\.com$'), re.compile(r'^.*abcnews\.go\.com$'), re.compile(r'^.*bbc\.co\.uk$'), re.compile(r'^.*usatoday\.com$'),
      re.compile(r'^.*latimes\.com$'), re.compile(r'^.*wired\.com$'), re.compile(r'^.*gizmodo\.com$'), re.compile(r'^.*mashable\.com$'),
      re.compile(r'^.*businessinsider\.com$'), re.compile(r'^.*macrumors\.com$'), re.compile(r'^.*engadget\.com$'),
      re.compile(r'^.*newyorker\.com$'),
    ]

    def resolves(self, parsed_url):
      return any([regex.match(parsed_url.hostname) for regex in self.hostname_regexes])

  class Uncategorized():
    category = 'uncategorized'

    def resolves(self, _):
      return True

  resolvers = [
    Site(), Repository(), WikiArticle(), AmazonItem(), Video(), Tweet(), Subreddit(), RedditPost(), News(),
    Uncategorized()  # Uncategorized must be last to catch uncategorized URLs.
  ]

  @timeout_decorator.timeout(15)
  def metafy_url(self, url):
    logger.info('Requesting {}'.format(url))
    try:
      response = requests.get(url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
    except:
      logger.exception('Could not reach {}'.format(url))
      return None

    if not response.status_code == 200 or not hasattr(response, 'text') or not response.text:
      return None

    parsed_url = urlparse(response.url)
    if parsed_url.path == '/' and not parsed_url.params and not parsed_url.query and not parsed_url.fragment:
      parsed_url = parsed_url._replace(path='')

    for resolver in self.resolvers:
      if resolver.resolves(parsed_url):
        return self._compose_metadata(parsed_url, response.text, resolver.category)

def metafy_urls(urls):
  metafier = Metafier()

  metafied_urls = []
  for url in tqdm(urls):
    try:
      matefied_url = metafier.metafy_url(url['url'])
    except timeout_decorator.TimeoutError:
      logger.exception('Timeout when metafying {}'.format(url['url']))
      continue

    if matefied_url:
      metafied_urls.append(dict(
        matefied_url, **{
          'item_id': url['item_id'],
          'item_url': url['item_url'],
          'root_id': url['root_id'],
          'original_title': url['original_title'],
        })
      )

  metafied_urls = list(filter(lambda x: x['title'] and x['url'], metafied_urls))
  metafied_urls = list({v['url']: v for v in metafied_urls}.values())

  return metafied_urls

def save(filename, links):
  json_data = {
      'timestamp': datetime.now(timezone.utc).astimezone().isoformat(),
      'links': links,
  }

  strip_control_characters = lambda s: ''.join(ch for ch in s if unicodedata.category(ch)[0] != 'C')
  json_string = strip_control_characters(json.dumps(json_data, ensure_ascii=False))

  with open(filename, 'w', encoding='utf-8') as f:
    f.write(json_string)

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--filename')
  args = parser.parse_args()

  urls = get_urls_from_top_stories(HackerNews(), 30)
  urls = filter_urls(urls)

  urls_metafied = metafy_urls(urls)

  if args.filename:
    save(args.filename, urls_metafied)
  else:
    logger.info(urls_metafied)

def test():
  print(metafy_urls([{
    'item_id': '19349830',
    'item_url': 'https://news.ycombinator.com/item?id=19349830',
    'root_id': '19349830',
    'original_title': 'Title',
    'url': '',
    }])
  )


# test()
main()
