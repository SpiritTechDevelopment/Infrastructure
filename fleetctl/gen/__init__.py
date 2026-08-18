"""Сгенерированные стабы вендоренных контрактов бэкенда.

Генерируется из contracts/<сервис>/v1/*.proto, руками не правится. Единственное
отличие от вывода protoc — импорт в *_pb2_grpc.py сделан относительным, иначе
модуль ищет соседний файл в корне sys.path.

Пересоздание::

    python -m grpc_tools.protoc -I contracts/manifest/v1 \\
        --python_out=fleetctl/gen --grpc_python_out=fleetctl/gen \\
        contracts/manifest/v1/manifest.proto

Версия рантайма protobuf на исполнителе обязана соответствовать версии
генератора: сгенерированный код проверяет её при импорте и падает на
несовпадении. Обе закреплены в platform_executor_python_packages.
"""
