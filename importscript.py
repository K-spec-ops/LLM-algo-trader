# Required packages for the GPT trader

import os
import sys
import yaml
import json
import boto3
import argparse
import warnings
import asyncio 
import threading
import logging
import subprocess
import numpy as np
from time import time, sleep
from ibapi.client import *
from ibapi.wrapper import *
from ibapi.contract import *
from ibapi.tag_value import TagValue
from botocore.exceptions import ClientError, CredentialRetrievalError, NoCredentialsError
from openai import OpenAI
from datetime import datetime
from colorama import Back, init, Style
from collections import defaultdict
from dataclasses import dataclass