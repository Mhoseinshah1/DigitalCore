COMPOSE ?= docker compose

.PHONY: install up down logs ps build seed migrate shell psql restart

install:        ## Run the interactive one-command installer
	./install.sh

up:             ## Start the stack
	$(COMPOSE) up -d

down:           ## Stop the stack
	$(COMPOSE) down

restart:        ## Restart the stack
	$(COMPOSE) restart

build:          ## Rebuild images
	$(COMPOSE) build

logs:           ## Tail all logs
	$(COMPOSE) logs -f

ps:             ## Show service status
	$(COMPOSE) ps

migrate:        ## Apply database migrations
	$(COMPOSE) run --rm web migrate

seed:           ## Re-run idempotent seeding
	$(COMPOSE) run --rm web seed

shell:          ## Open a shell in the web image
	$(COMPOSE) run --rm web shell

psql:           ## Open a psql session
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-digitalcore} -d $${POSTGRES_DB:-digitalcore}
