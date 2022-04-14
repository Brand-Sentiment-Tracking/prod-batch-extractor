import os
import logging

from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from datetime import datetime

from pyspark import SparkConf, SparkContext
from pyspark.sql import SparkSession, Row, types

from extractor.extractor import CommonCrawlArticleExtractor
from newspaper import Article


class ArticleToParquetS3:
    """Upload extracted articles from CC-NEWS to Amazon S3 as Parquet files.

    Note:
        Credentials for AWS are assumed to be held as environment variables.
        Make sure they are provided either by exporting `AWS_ACCESS_KEY_ID`
        and `AWS_SECRET_ACCESS_KEY`.

    Args:
        bucket (str): The name of the bucket on S3 to push to.
        parquet_file (str): The filepath from the S3 bucket root to the
            Parquet file to push to.
        batch_size (int): The number of articles to extract before pushing as
            a batch.
        partitions (Tuple[str]): The set of keys to partition the parquet file
            by. All available keys can be found in `self.KEYS`.
    """
    KEYS = ("title", "main_text", "url", "source_domain", "date_publish",
            "date_crawled", "language")

    def __init__(self, bucket: str, parquet_file: str,
                 local_parquet_dir: str = "./parquet",
                 partitions: Optional[Tuple[str]] = None,
                 batch_size: Optional[int] = 1000,
                 report_every: Optional[int] = 1000):

        self.__bucket = None
        self.__parquet_file = None

        self.bucket = bucket

        self.parquet_file = parquet_file
        self.local_parquet_dir = local_parquet_dir

        self.partitions = partitions if partitions is not None \
            else ("date_crawled", "language")

        self.batch_size = batch_size
        self.report_every = report_every

        self.extractor = CommonCrawlArticleExtractor(self.add_article)

        self.spark = SparkSession.builder \
            .appName("ArticleToParquet") \
            .getOrCreate()

        self.context = SparkContext.getOrCreate(SparkConf())

        self.schema = types.StructType([
            types.StructField('title', types.StringType(), True),
            types.StructField('main_text', types.StringType(), True),
            types.StructField('url', types.StringType(), False),
            types.StructField('source_domain', types.StringType(), False),
            types.StructField('date_publish', types.StringType(), True),
            types.StructField('date_crawled', types.StringType(), False),
            types.StructField('language', types.StringType(), False)
        ])

        self.articles = list()

    @property
    def bucket(self) -> str:
        """`str`: The S3 bucket name to push the parquet files."""
        return self.__bucket

    @bucket.setter
    def bucket(self, name: str):
        if type(name) != str:
            raise ValueError("Bucket name is not a string.")

        self.__bucket = name

    @property
    def parquet_file(self) -> str:
        return self.__parquet_file

    @parquet_file.setter
    def parquet_file(self, filename: str):
        if type(filename) != str:
            raise ValueError("S3 Parquet file is not a string.")

        self.__parquet_file = filename

    @property
    def local_parquet_dir(self) -> str:
        return self.__local_parquet_dir

    @local_parquet_dir.setter
    def local_parquet_dir(self, path: str):
        if type(path) != str:
            raise ValueError("Path is not a string.")
        
        if not os.path.exists(path):
            logging.info(f"Making new local parquet directory '{path}'.")
            os.mkdir(path)
        
        self.__local_parquet_dir = path

    @property
    def parquet_url(self) -> str:
        """`str`: The Amazon S3 URL to the parquet file to push to."""
        return f"s3a://{self.bucket}/{self.parquet_file}"

    @property
    def local_parquet_file(self) -> str:
        """`str`: The path to save temporary parquet files to locally."""
        return os.path.join(self.local_parquet_dir, self.parquet_file)

    @property
    def partitions(self) -> Tuple[str]:
        """`Tuple[str]`: The keys to partition the parquet file by in S3.

        The setter will raise a ValueError if the new keys are not a tuple of
        strings or one of the keys doesn't exist in the dataframe.
        """
        return self.__partitions

    @partitions.setter
    def partitions(self, keys: Tuple[str]):
        if type(keys) != tuple:
            raise ValueError("Partition keys is not a tuple.")
        elif any(map(lambda k: type(k) != str, keys)):
            raise ValueError("Not all keys are strings.")
        elif any(map(lambda k: k not in self.KEYS, keys)):
            raise ValueError("One of the keys doesn't exist.")

        self.__partitions = keys

    @property
    def batch_size(self) -> Optional[int]:
        """`int`: The number of articles to download to disk in batches.
        
        The setter will raise a ValueError if the new batch size is not an
        integer greater than zero.
        """
        return self.__batch_size

    @batch_size.setter
    def batch_size(self, size: Optional[int]):
        if size is not None and (type(size) != int or size <= 0):
            raise ValueError("Size is not an integer greater than zero.")

        self.__batch_size = size

    @property
    def report_every(self) -> Optional[int]:
        """`int`: The number of articles processed per counter report.
        
        Setter will raise a ValueError if the new value is not an integer
        greater than zero.
        """
        return self.__report_every

    def report_every(self, n: Optional[int]):
        if n is not None and (type(n) != int or n <= 0):
            raise ValueError("Value must be an integer greater than zero.")

        self.__report_every = n

    def report_counters(self):
        """Report the extracted/discarded/errored/total counters."""
        message = "CounterReport:"
        for name, counter in self.extractor.counters.items():
            message += f" {name}={counter}"

        logging.info(message)

    @staticmethod
    def __dict_to_row(article: OrderedDict) -> Row:
        """Convert an article as an `OrderedDict` into a Spark `Row` object.

        Args:
            article (OrderedDict): The article to convert to a Row object.

        Returns:
            Row: The converted article.
        """
        return Row(**article)

    def add_article(self, article: Article, date_crawled: datetime,
                    counters: Dict[str, int]):
        try:
            date_published = article.publish_date.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            date_published = None

        self.articles.append(
            OrderedDict([
                ("title", article.title),
                ("main_text", article.text),
                ("url", article.url),
                ("source_domain", article.source_url),
                ("date_publish", date_published),
                ("date_crawled", date_crawled.strftime("%Y-%m-%d")),
                ("language", article.config.get_language())
            ])
        )

        if self.batch_size is not None \
            and counters["extracted"] % self.batch_size == 0:

            self.save_parquet_locally()

        if self.report_every is not None \
            and counters["extracted"] % self.report_every == 0:
            
            self.report_counters()

    def save_parquet_locally(self):
        if not self.articles:
            logging.info("No articles available to save.")
            return

        rows = self.context \
            .parallelize(self.articles) \
            .map(self.__dict_to_row)

        articles_df = self.spark.createDataFrame(rows, self.schema)
        
        logging.info(f"Saving parque to '{self.local_parquet_file}'")

        articles_df.repartition(*self.partitions) \
            .write.mode('append') \
            .partitionBy(*self.partitions) \
            .parquet(self.local_parquet_file)
        
        self.articles = list()

    def upload_parquet_to_s3(self):
        articles_df = self.spark.read.parquet(self.local_parquet_file)

        logging.info(f"Pushing to '{self.parquet_url}'")

        articles_df.repartition(*self.partitions) \
            .write.mode('append') \
            .partitionBy(*self.partitions) \
            .parquet(self.parquet_url)

    def run(self, patterns: List[str], start_date: datetime,
            end_date: datetime):

        self.extractor.download_articles(patterns, start_date, end_date)
        
        # Download any remaining articles and bulk upload to S3
        self.save_parquet_locally()
        self.upload_parquet_to_s3()
