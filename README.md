# GitHub Keyword Crawler

## Running locally :computer:

You will need Python 3.10 or later, MongoDB 6.0.X, and preferably a GitHub account[^1].

First install the required libraries via the Python package installer:

```shell
pip install -r requirements.txt
```

Next, make sure that MongoDB is up and running:

```shell
lsof | grep mongod
```

With the packages installed and database running, you can start the mining:

```shell
python3 main.py --token {gh_pat} --target {target} {keyword}
```

Where:
- `gh_pat`: Your personal GitHub access token (`ghp_\w{36}`), with `repo` read privileges;
- `target`: The target endpoint (`commits`, `issues` or `pull-requests`);
- `keyword`: The term to search for.

Note that `--token` is an optional parameter, and can be supplied alternatively via the `GITHUB_TOKEN` environment variable.
If you need to further configure the MongoDB host and port settings, you can use the `DATABASE_HOST` and `DATABASE_PORT` environment variables respectively.
Mined data is stored in a database whose name corresponds to the provided `keyword`, split across collections for each of the three target endpoints.

## Running on Docker :whale:

Assuming you have the latest versions of both Docker and Docker Compose, we provide a ready configuration to jump-start the mining.

```shell
docker-compose -f deployment/docker-compose.yml up gh-keyword-crawler-{target} -d
```

Substituting `target` with one of the aforementioned options.
Note that there are two methods of configuring your own GitHub access token:

1. Creating a `.env` file in [`deployment`](/deployment) with a `GITHUB_TOKEN` entry:
    ```dotenv
    GITHUB_TOKEN=#your token goes here
    ```
2. Creating a `docker-compose.override.yml` file in [`deployment`](/deployment), for example:
    ```yaml
    version: '3.9'

    services:
    
      gh-keyword-crawler-commits:
        environment:
          GITHUB_TOKEN: # A token for commits
    
      gh-keyword-crawler-issues:
        environment:
          GITHUB_TOKEN: # A token for issues
    
      gh-keyword-crawler-pull-requests:
        environment:
          GITHUB_TOKEN: # A token for pull-requests
    ```

If you employ the second approach, you must also provide the override file as an argument:

```shell
docker-compose -f deployment/docker-compose.yml -f deployment/docker-compose.override.yml up gh-keyword-crawler-{target} -d
```

The advantage of this approach is that it allows us to define separate access tokens for distinct endpoints,
and run their miners in parallel:

```shell
docker-compose -f deployment/docker-compose.yml -f deployment/docker-compose.override.yml up -d
```

Regardless of how you deploy, the database data will be kept in the `gh-keyword-crawler-data` volume.
Running crawler logs are bound to the `deployment/logs/{target}` directory.

[^1]: While the account is not mandatory, the mining will be performed significantly faster if a personal access token (PAT) is provided.