# CK-CADForge —— CAD 轻量化转换服务
#
# 独立镜像。不要把它的内容复制进 CK-PLM 的镜像 ——
# 本项目是 LGPL-3.0（依赖 pythonocc-core），CK-PLM 是 Apache-2.0，
# 混合分发会让许可证边界失效。

FROM continuumio/miniconda3:latest

WORKDIR /app

# 系统依赖：OCCT 的部分模块需要 OpenGL/Mesa 头与运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1-mesa-glx libglu1-mesa libxrender1 libxft2 \
    && rm -rf /var/lib/apt/lists/*

# pythonocc-core 只能从 conda-forge 装（依赖 OCCT 原生库）
# 这一层最慢，放在依赖之前可以利用构建缓存
# 已在 7.7.2 上实测跑通完整管线；固定小版本，避免 SWIG 绑定变动带来的静默行为差异
RUN conda create -y -n cadforge python=3.10 \
    && conda install -y -n cadforge -c conda-forge pythonocc-core=7.7.2 \
    && conda clean -afy

# 让容器默认使用该环境
ENV PATH=/opt/conda/envs/cadforge/bin:$PATH

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.cloud.tencent.com/pypi/simple

COPY app ./app
COPY tests ./tests

# 每个转换任务跑在独立子进程里：OCCT 段错误会直接杀进程，
# 进程隔离是唯一的兜底手段（Python 的 try/except 接不住）
ENV CADFORGE_OUTPUT_DIR=/data/ck-plm/cad/visualizations
ENV CADFORGE_JOB_TIMEOUT=300
ENV CADFORGE_MAX_CONCURRENCY=2
ENV PORT=8090

# 产物目录建议挂与 CK-PLM 相同的卷，避免大文件走 HTTP
VOLUME ["/data/ck-plm"]

EXPOSE 8090

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
