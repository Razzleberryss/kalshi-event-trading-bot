"""Setup configuration for kalshi-event-trading-bot."""

from setuptools import setup, find_packages

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

with open("requirements.txt", encoding="utf-8") as f:
    requirements = [
        line.strip() for line in f if line.strip() and not line.startswith("#")
    ]

setup(
    name="kalshi-event-trading-bot",
    version="0.1.0",
    author="Razzleberryss",
    description="Async Python bot for automated event trading on Kalshi prediction markets",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Razzleberryss/kalshi-event-trading-bot",
    packages=find_packages(exclude=["tests*"]),
    python_requires=">=3.10",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=8.0.0",
            "pytest-asyncio>=0.23.0",
            "pytest-cov>=5.0.0",
            "pytest-mock>=3.14.0",
            "ruff>=0.4.0",
            "mypy>=1.9.0",
            "bandit>=1.7.8",
        ],
    },
    entry_points={
        "console_scripts": [
            "kalshi-bot=app.main:main",
            "kalshi-backtest=sim.backtest_cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Financial and Insurance Industry",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial :: Investment",
        "Operating System :: OS Independent",
    ],
    keywords=[
        "kalshi",
        "prediction-markets",
        "trading-bot",
        "event-trading",
        "asyncio",
        "paper-trading",
    ],
)
