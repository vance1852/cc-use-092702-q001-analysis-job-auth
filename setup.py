"""国家森林公园资源、生态监测与风险治理服务的本地安装入口。"""
from setuptools import find_packages, setup
setup(name="natural-history-operations", version="0.1.0", description="自然史标本保藏、实验复核与生物安全协作服务", long_description=open("README.md", encoding="utf-8").read(), long_description_content_type="text/markdown", package_dir={"": "src"}, packages=find_packages("src"), python_requires=">=3.11")
