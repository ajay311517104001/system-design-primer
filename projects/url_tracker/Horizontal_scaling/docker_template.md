services:

  # YOUR APP
  api:
    build: .
    container_name: my_api
    env_file: .env               # ← secrets in .env file, not hardcoded here
    environment:
      - DATABASE_URL=postgresql://user:pass@db:5432/mydb
      - REDIS_URL=redis://cache:6379
    depends_on:
      - db
      - cache
    networks:
      - app_net

  # DATABASE
  db:
    image: postgres:16-alpine
    container_name: my_db
    environment:
      - POSTGRES_USER=user
      - POSTGRES_PASSWORD=pass
      - POSTGRES_DB=mydb
    volumes:
      - pg_data:/var/lib/postgresql/data   # persist DB across restarts
    networks:
      - app_net

  # CACHE
  cache:
    image: redis:7-alpine
    container_name: my_cache
    networks:
      - app_net

  # LOAD BALANCER (add only when you have multiple app replicas)
  nginx:
    image: nginx:alpine
    container_name: my_lb
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - api
    networks:
      - app_net

volumes:
  pg_data:

networks:
  app_net:
    driver: bridge
