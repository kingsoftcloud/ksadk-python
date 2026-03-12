FROM python:3.12-slim

WORKDIR /app

# 只拷贝宣传页面需要的关键内容，并将其重命名为 index.html 使得根目录默认展示主页
COPY docs/openclaw_client_one_click_deploy.html /app/index.html
COPY docs/openclaw_gateway_technical.html /app/
COPY docs/preview/ /app/preview/

# 替换两个页面间的双向跳转链接，保证打包后的导航栏不报错
RUN sed -i 's/openclaw_client_one_click_deploy.html/index.html/g' /app/index.html && \
    sed -i 's/openclaw_client_one_click_deploy.html/index.html/g' /app/openclaw_gateway_technical.html

EXPOSE 8000

# 启动静态 Web 服务
CMD ["python", "-m", "http.server", "8000"]
